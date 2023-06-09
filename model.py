import torch
import clip
import torch.nn as nn
import torch.nn.init
import torchvision.models as models
from torch.autograd import Variable
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence
import torch.backends.cudnn as cudnn
from torch.nn.utils.clip_grad import clip_grad_norm
import numpy as np
from collections import OrderedDict
import torch.nn.functional as F
from GCN_lib.Rs_GCN import Rs_GCN

import opts
import misc.utils as utils
import torch.optim as optim


from models import DecoderRNN, EncoderRNN, S2VTAttModel, S2VTModel
from torch import nn



def l2norm(X):
    """L2-normalize columns of X
    """
    norm = torch.pow(X, 2).sum(dim=1, keepdim=True).sqrt()
    X = torch.div(X, norm)
    return X


def EncoderImage(data_name, img_dim, embed_size, finetune=False,
                 cnn_type='vgg19', text_number = 15, text_dim = 300, use_abs=False, no_imgnorm=False, use_txt_emb = True):
    """A wrapper to image encoders. Chooses between an encoder that uses
    precomputed image features, `EncoderImagePrecomp`, or an encoder that
    computes image features on the fly `EncoderImageFull`.
    """
    if data_name.endswith('_precomp'):
        if use_txt_emb == True:
            # USED FOR SCENE TEXT FEATURES
            img_enc = EncoderImagePrecompAttn(
                img_dim, embed_size, data_name, text_number, text_dim, use_abs, no_imgnorm)
        else:
            img_enc = EncoderImagePrecomp(
                img_dim, embed_size, use_abs, no_imgnorm)
    else:
        img_enc = EncoderImageFull(
            embed_size, finetune, cnn_type, use_abs, no_imgnorm)

    return img_enc


# tutorials/09 - Image Captioning
class EncoderImageFull(nn.Module):

    def __init__(self, embed_size, finetune=False, cnn_type='vgg19',
                 use_abs=False, no_imgnorm=False):
        """Load pretrained VGG19 and replace top fc layer."""
        super(EncoderImageFull, self).__init__()
        self.embed_size = embed_size
        self.no_imgnorm = no_imgnorm
        self.use_abs = use_abs

        # Load a pre-trained model
        self.cnn = self.get_cnn(cnn_type, True)

        # For efficient memory usage.
        for param in self.cnn.parameters():
            param.requires_grad = finetune

        # Replace the last fully connected layer of CNN with a new one
        if cnn_type.startswith('vgg'):
            self.fc = nn.Linear(self.cnn.classifier._modules['6'].in_features,
                                embed_size)
            self.cnn.classifier = nn.Sequential(
                *list(self.cnn.classifier.children())[:-1])
        elif cnn_type.startswith('resnet'):
            self.fc = nn.Linear(self.cnn.module.fc.in_features, embed_size)
            self.cnn.module.fc = nn.Sequential()

        self.init_weights()

    def get_cnn(self, arch, pretrained):
        """Load a pretrained CNN and parallelize over GPUs
        """
        if pretrained:
            print("=> using pre-trained model '{}'".format(arch))
            model = models.__dict__[arch](pretrained=True)
        else:
            print("=> creating model '{}'".format(arch))
            model = models.__dict__[arch]()

        if arch.startswith('alexnet') or arch.startswith('vgg'):
            model.features = nn.DataParallel(model.features)
            model.cuda()
        else:
            model = nn.DataParallel(model).cuda()

        return model

    def load_state_dict(self, state_dict):
        """
        Handle the models saved before commit pytorch/vision@989d52a
        """
        if 'cnn.classifier.1.weight' in state_dict:
            state_dict['cnn.classifier.0.weight'] = state_dict[
                'cnn.classifier.1.weight']
            del state_dict['cnn.classifier.1.weight']
            state_dict['cnn.classifier.0.bias'] = state_dict[
                'cnn.classifier.1.bias']
            del state_dict['cnn.classifier.1.bias']
            state_dict['cnn.classifier.3.weight'] = state_dict[
                'cnn.classifier.4.weight']
            del state_dict['cnn.classifier.4.weight']
            state_dict['cnn.classifier.3.bias'] = state_dict[
                'cnn.classifier.4.bias']
            del state_dict['cnn.classifier.4.bias']

        super(EncoderImageFull, self).load_state_dict(state_dict)

    def init_weights(self):
        """Xavier initialization for the fully connected layer
        """
        r = np.sqrt(6.) / np.sqrt(self.fc.in_features +
                                  self.fc.out_features)
        self.fc.weight.data.uniform_(-r, r)
        self.fc.bias.data.fill_(0)

    def forward(self, images):
        """Extract image feature vectors."""
        features = self.cnn(images)

        # normalization in the image embedding space
        features = l2norm(features)

        # linear projection to the joint embedding space
        features = self.fc(features)

        # normalization in the joint embedding space
        if not self.no_imgnorm:
            features = l2norm(features)

        # take the absolute value of the embedding (used in order embeddings)
        if self.use_abs:
            features = torch.abs(features)

        return features


class EncoderImagePrecomp(nn.Module):

    def __init__(self, img_dim, embed_size, use_abs=False, no_imgnorm=False):
        super(EncoderImagePrecomp, self).__init__()
        self.embed_size = embed_size
        self.no_imgnorm = no_imgnorm
        self.use_abs = use_abs

        self.fc = nn.Linear(img_dim, embed_size)

        self.init_weights()



    def init_weights(self):
        """Xavier initialization for the fully connected layer
        """
        r = np.sqrt(6.) / np.sqrt(self.fc.in_features +
                                  self.fc.out_features)
        self.fc.weight.data.uniform_(-r, r)
        self.fc.bias.data.fill_(0)

    def forward(self, images):
        """Extract image feature vectors."""
        # assuming that the precomputed features are already l2-normalized

        # print(images)
        # images = images.view(images.size(0), 73728)
        features = self.fc(images)
        
        # normalize in the joint embedding space
        if not self.no_imgnorm:
            features = l2norm(features)

        # take the absolute value of embedding (used in order embeddings)
        if self.use_abs:
            features = torch.abs(features)

        return features

    def load_state_dict(self, state_dict):
        """Copies parameters. overwritting the default one to
        accept state_dict from Full model
        """
        own_state = self.state_dict()
        new_state = OrderedDict()
        for name, param in state_dict.items():
            if name in own_state:
                new_state[name] = param

        super(EncoderImagePrecomp, self).load_state_dict(new_state)



class EncoderImagePrecompAttn(nn.Module):

    def __init__(self, img_dim, embed_size, data_name, text_number, text_dim, use_abs=False, no_imgnorm=False):
        super(EncoderImagePrecompAttn, self).__init__()
        self.embed_size = embed_size
        self.no_imgnorm = no_imgnorm
        self.use_abs = use_abs
        self.data_name = data_name

        self.fc = nn.Linear(img_dim, embed_size)
        self.init_weights()


        # GSR
        self.img_rnn = nn.GRU(embed_size, embed_size, 1, batch_first=True)

        # GCN reasoning
        self.Rs_GCN_1 = Rs_GCN(in_channels=embed_size, inter_channels=embed_size)
        self.Rs_GCN_2 = Rs_GCN(in_channels=embed_size, inter_channels=embed_size)
        self.Rs_GCN_3 = Rs_GCN(in_channels=embed_size, inter_channels=embed_size)
        self.Rs_GCN_4 = Rs_GCN(in_channels=embed_size, inter_channels=embed_size)

        if self.data_name == 'f30k_precomp':
            self.bn = nn.BatchNorm1d(embed_size)

        # FOR SCENE TEXT FEATURES
        self.bn_scene_text = nn.BatchNorm1d(text_number)# 对应的是20 * 300(fasttext) + 2048(fasterrcnn_feature)
        self.fc_scene_text = nn.Linear(300+2048, embed_size)

        # GCN reasoning
        self.Text_GCN_1 = Rs_GCN(in_channels=embed_size, inter_channels=embed_size)
        self.Text_GCN_2 = Rs_GCN(in_channels=embed_size, inter_channels=embed_size)
        self.Text_GCN_3 = Rs_GCN(in_channels=embed_size, inter_channels=embed_size)
        self.Text_GCN_4 = Rs_GCN(in_channels=embed_size, inter_channels=embed_size)
  
    def init_weights(self):
        """Xavier initialization for the fully connected layer
        """
        r = np.sqrt(6.) / np.sqrt(self.fc.in_features +
                                  self.fc.out_features)
        self.fc.weight.data.uniform_(-r, r)
        self.fc.bias.data.fill_(0)


    def forward(self, images, scene_text):
        """Extract image feature vectors."""

        # IMAGE FEATURES
        fc_img_emd = self.fc(images)
        


        
        if self.data_name != 'f30k_precomp':
            fc_img_emd = l2norm(fc_img_emd)


        # fc_img_emd = torch.cat((fc_img_emd, fc_scene_text), dim=1)

        # GCN reasoning
        # -> B,D,N
        GCN_img_emd = fc_img_emd.permute(0, 2, 1)
        GCN_img_emd = self.Rs_GCN_1(GCN_img_emd)
        GCN_img_emd = self.Rs_GCN_2(GCN_img_emd)
        GCN_img_emd = self.Rs_GCN_3(GCN_img_emd)
        GCN_img_emd = self.Rs_GCN_4(GCN_img_emd)

        # -> B,N,D
        GCN_img_emd = GCN_img_emd.permute(0, 2, 1)
        GCN_img_emd = l2norm(GCN_img_emd)
       

        rnn_img, hidden_state = self.img_rnn(GCN_img_emd)
        visual_features = hidden_state[0]

        

        
        # SCENE TEXT FEATURES --- AM --------
        fc_scene_text = self.bn_scene_text(scene_text)# 归一化之后的
        fc_scene_text = F.leaky_relu(self.fc_scene_text(fc_scene_text))
        fc_scene_text = l2norm(fc_scene_text)

        # Scene Text Reasoning
        # -> B,D,N
        GCN_scene_text_emd = fc_scene_text.permute(0, 2, 1)
        GCN_scene_text_emd = self.Text_GCN_1(GCN_scene_text_emd)
        GCN_scene_text_emd = self.Text_GCN_2(GCN_scene_text_emd)
        GCN_scene_text_emd = self.Text_GCN_3(GCN_scene_text_emd)
        GCN_scene_text_emd = self.Text_GCN_4(GCN_scene_text_emd)
        # # -> B,N,D
        GCN_scene_text_emd = GCN_scene_text_emd.permute(0, 2, 1)
        GCN_scene_text_emd = l2norm(GCN_scene_text_emd)
        fc_scene_text = torch.mean(GCN_scene_text_emd, dim=1)
        

        
        # FINAL AGGREGATION
        # fc_scene_text = torch.mean(fc_scene_text, dim=1)
        features = torch.mul(visual_features, fc_scene_text) + visual_features

        # features = torch.mean(GCN_img_emd, dim=1)

        if self.data_name == 'f30k_precomp':
            features = self.bn(features)

        # normalize in the joint embedding space
        if not self.no_imgnorm:
            features = l2norm(features)

        # take the absolute value of embedding (used in order embeddings)
        if self.use_abs:
            features = torch.abs(features)

        return features, GCN_img_emd, visual_features# add visual_features for loss_calculation
 
    def load_state_dict(self, state_dict):
        """Copies parameters. overwritting the default one to
        accept state_dict from Full model
        """
        own_state = self.state_dict()
        new_state = OrderedDict()
        for name, param in state_dict.items():
            if name in own_state:
                new_state[name] = param

        super(EncoderImagePrecompAttn, self).load_state_dict(new_state)


# tutorials/08 - Language Model
# RNN Based Language Model
class EncoderText(nn.Module):

    def __init__(self, vocab_size, word_dim, embed_size, num_layers,
                 use_abs=False):
        super(EncoderText, self).__init__()
        self.use_abs = use_abs
        self.embed_size = embed_size

        # word embedding
        self.embed = nn.Embedding(vocab_size, word_dim)

        # caption embedding
        self.rnn = nn.GRU(word_dim, embed_size, num_layers, batch_first=True)

        self.init_weights()


    def init_weights(self):
        self.embed.weight.data.uniform_(-0.1, 0.1)

    def forward(self, x, lengths):
        """Handles variable size captions
        """
        # Embed word ids to vectors
        x = self.embed(x)
        packed = pack_padded_sequence(x, lengths, batch_first=True)


        # Forward propagate RNN
        out, _ = self.rnn(packed)


        # Reshape *final* output to (batch_size, hidden_size)
        padded = pad_packed_sequence(out, batch_first=True)
        I = torch.LongTensor(lengths).view(-1, 1, 1)
        I = Variable(I.expand(x.size(0), 1, self.embed_size)-1).cuda()
        out = torch.gather(padded[0], 1, I).squeeze(1)

        # normalization in the joint embedding space
        out = l2norm(out)

        # take absolute value, used by order embeddings
        if self.use_abs:
            out = torch.abs(out)

        return out


def cosine_sim(im, s):
    """Cosine similarity between all the image and sentence pairs
    """
    return im.mm(s.t())


def order_sim(im, s):
    """Order embeddings similarity measure $max(0, s-im)$
    """
    YmX = (s.unsqueeze(1).expand(s.size(0), im.size(0), s.size(1))
           - im.unsqueeze(0).expand(s.size(0), im.size(0), s.size(1)))
    score = -YmX.clamp(min=0).pow(2).sum(2).sqrt().t()
    return score


class ContrastiveLoss(nn.Module):
    """
    Compute contrastive loss
    """

    def __init__(self, margin=0, measure=False, max_violation=False):
        super(ContrastiveLoss, self).__init__()
        self.margin = margin
        if measure == 'order':
            self.sim = order_sim
        else:
            self.sim = cosine_sim

        self.max_violation = max_violation

    def forward(self, im, s):
        # compute image-sentence score matrix
        scores = self.sim(im, s)
        diagonal = scores.diag().view(im.size(0), 1)
        d1 = diagonal.expand_as(scores)
        d2 = diagonal.t().expand_as(scores)

        # compare every diagonal score to scores in its column
        # caption retrieval
        cost_s = (self.margin + scores - d1).clamp(min=0)
        # compare every diagonal score to scores in its row
        # image retrieval
        cost_im = (self.margin + scores - d2).clamp(min=0)

        # clear diagonals
        mask = torch.eye(scores.size(0)) > .5
        I = Variable(mask)
        if torch.cuda.is_available():
            I = I.cuda()
        cost_s = cost_s.masked_fill_(I, 0)
        cost_im = cost_im.masked_fill_(I, 0)

        # keep the maximum violating negative for each query
        if self.max_violation:
            cost_s = cost_s.max(1)[0]
            cost_im = cost_im.max(0)[0]

        return cost_s.sum() + cost_im.sum()


class VSRN(object):
    """
    rkiros/uvs model
    """

    def __init__(self, opt):
        # tutorials/09 - Image Captioning
        # Build Models
        self.grad_clip = opt.grad_clip
        self.img_enc = EncoderImage(opt.data_name, opt.img_dim, opt.embed_size,
                                    opt.finetune, opt.cnn_type, opt.text_number, opt.text_dim,
                                    use_abs=opt.use_abs,
                                    no_imgnorm=opt.no_imgnorm)

        self.txt_enc = EncoderText(opt.vocab_size, opt.word_dim,
                                   opt.embed_size, opt.num_layers,
                                   use_abs=opt.use_abs)
        if torch.cuda.is_available():
            self.img_enc.cuda()
            self.txt_enc.cuda()
            cudnn.benchmark = True



        #####   captioning elements

        self.encoder = EncoderRNN(
            opt.dim_vid,
            opt.dim_hidden,
            bidirectional=opt.bidirectional,
            input_dropout_p=opt.input_dropout_p,
            rnn_cell=opt.rnn_type,
            rnn_dropout_p=opt.rnn_dropout_p)
        self.decoder = DecoderRNN(
            opt.vocab_size,
            opt.max_len,
            opt.dim_hidden,
            opt.dim_word,
            input_dropout_p=opt.input_dropout_p,
            rnn_cell=opt.rnn_type,
            rnn_dropout_p=opt.rnn_dropout_p,
            bidirectional=opt.bidirectional)

        self.caption_model = S2VTAttModel(self.encoder, self.decoder)

        self.crit = utils.LanguageModelCriterion()
        self.rl_crit = utils.RewardCriterion()

        if torch.cuda.is_available():
            self.caption_model.cuda()


        # Loss and Optimizer
        self.criterion = ContrastiveLoss(margin=opt.margin,
                                         measure=opt.measure,
                                         max_violation=opt.max_violation)
        params = list(self.txt_enc.parameters())
        params += list(self.img_enc.parameters())
        params += list(self.decoder.parameters())
        params += list(self.encoder.parameters())
        params += list(self.caption_model.parameters())

        if opt.finetune:
            params += list(self.img_enc.cnn.parameters())
        self.params = params

        self.optimizer = torch.optim.Adam(params, lr=opt.learning_rate)

        self.Eiters = 0



    def calcualte_caption_loss(self, fc_feats, labels, masks):

        # labels = Variable(labels, volatile=False)
        # masks = Variable(masks, volatile=False)

        torch.cuda.synchronize()
        labels = labels.cuda()
        masks = masks.cuda()

        # if torch.cuda.is_available():
        #     labels.cuda()
        #     masks.cuda()

        seq_probs, _ = self.caption_model(fc_feats, labels, 'train')
        loss = self.crit(seq_probs, labels[:, 1:], masks[:, 1:])


        return loss


    def state_dict(self):
        state_dict = [self.img_enc.state_dict(), self.txt_enc.state_dict()]
        return state_dict

    def load_state_dict(self, state_dict):
        self.img_enc.load_state_dict(state_dict[0])
        self.txt_enc.load_state_dict(state_dict[1])

    def train_start(self):
        """switch to train mode
        """
        self.img_enc.train()
        self.txt_enc.train()

    def val_start(self):
        """switch to evaluate mode
        """
        self.img_enc.eval()
        self.txt_enc.eval()

    def forward_emb(self, images, captions, lengths, scene_text, volatile=False):
        """Compute the image and caption embeddings
        """
        # Set mini-batch dataset

        images = Variable(images)
        captions = Variable(captions)
        scene_text = Variable(scene_text)
        if torch.cuda.is_available():
            images = images.cuda()
            captions = captions.cuda()
            scene_text = scene_text.cuda()

        # Forward

        cap_emb = self.txt_enc(captions, lengths)
        img_emb, GCN_img_emd, visual_features = self.img_enc(images, scene_text)#the img_emb has fusion with the scene-text ocr##################################
        return img_emb, cap_emb, GCN_img_emd, visual_features

    def forward_loss(self, img_emb, cap_emb, **kwargs):
        """Compute the loss given pairs of image and caption embeddings
        """
        loss = self.criterion(img_emb, cap_emb)
        # self.logger.update('Le', loss.data[0], img_emb.size(0)) loss.data[0] is deprecated in pytorch --version >4.0 using loss.item() instead
        self.logger.update('Le_retrieval', loss.item(), img_emb.size(0))
        return loss
    
    def forward_loss_add_new(self, visual_features, cap_emb, **kwargs):
        """Compute the loss given pairs of image and caption embeddings
        """
        loss_add_new = self.criterion(visual_features, cap_emb)
        # add new loss
        self.logger.update('loss_add_new_retrieval', loss_add_new.item(), visual_features.size(0))
        return loss_add_new
    
    def train_emb(self, images, captions, lengths, ids, caption_labels, caption_masks, scene_text, *args):
        """One training step given images and captions.
        """
        self.Eiters += 1
        self.logger.update('Eit', self.Eiters)
        self.logger.update('lr', self.optimizer.param_groups[0]['lr'])

        # compute the embeddings
        img_emb, cap_emb, GCN_img_emd, visual_features = self.forward_emb(images, captions, lengths, scene_text)


        # calcualte captioning loss
        self.optimizer.zero_grad()

        caption_loss = self.calcualte_caption_loss(GCN_img_emd, caption_labels, caption_masks)


        # measure accuracy and record loss
        self.optimizer.zero_grad()
        retrieval_loss = self.forward_loss(img_emb, cap_emb)
        add_new_loss = self.forward_loss_add_new(visual_features, cap_emb)

        loss = 2.0 * retrieval_loss + caption_loss ############# tried 1.0, 0.8 0.5 0.2 0.05


        self.logger.update('Le_caption', caption_loss.item(), img_emb.size(0))
        self.logger.update('Add new loss', add_new_loss.item(), img_emb.size(0))
        self.logger.update('Le', loss.item(), img_emb.size(0))


        # compute gradient and do SGD step
        loss.backward()
        if self.grad_clip > 0:
            clip_grad_norm(self.params, self.grad_clip)
        self.optimizer.step()
