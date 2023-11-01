from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import copy
import logging
import math

from os.path import join as pjoin


from scipy import ndimage

import torch
import torch.nn as nn
import numpy as np

from torch.nn import CrossEntropyLoss, Dropout, Softmax, Linear, Conv2d, LayerNorm
from torch.nn.modules.utils import _pair

import models_vit.configs as configs

from .modeling_resnet import ResNetV2


logger = logging.getLogger(__name__)


ATTENTION_Q = "MultiHeadDotProductAttention_1/query"
ATTENTION_K = "MultiHeadDotProductAttention_1/key"
ATTENTION_V = "MultiHeadDotProductAttention_1/value"
ATTENTION_OUT = "MultiHeadDotProductAttention_1/out"
FC_0 = "MlpBlock_3/Dense_0"
FC_1 = "MlpBlock_3/Dense_1"
ATTENTION_NORM = "LayerNorm_0"
MLP_NORM = "LayerNorm_2"


def np2th(weights, conv=False):
    """Possibly convert HWIO to OIHW."""
    if conv:
        weights = weights.transpose([3, 2, 0, 1])
    return torch.from_numpy(weights)


def swish(x):
    return x * torch.sigmoid(x)


ACT2FN = {"gelu": torch.nn.functional.gelu, "relu": torch.nn.functional.relu, "swish": swish}


class Attention(nn.Module): # https://github.com/huggingface/transformers/blob/ebee0a27940adfbb30444d83387b9ea0f1173f40/src/transformers/models/bert/modeling_bert.py#L344
    def __init__(self, config, vis):
        super(Attention, self).__init__()
        self.vis = vis
        self.num_attention_heads = config.transformer["num_heads"]
        self.attention_head_size = int(config.hidden_size / self.num_attention_heads)
        self.all_head_size = self.num_attention_heads * self.attention_head_size

        self.query = Linear(config.hidden_size, self.all_head_size)
        self.key = Linear(config.hidden_size, self.all_head_size)
        self.value = Linear(config.hidden_size, self.all_head_size)

        self.out = Linear(config.hidden_size, config.hidden_size)
        self.attn_dropout = Dropout(config.transformer["attention_dropout_rate"])
        self.proj_dropout = Dropout(config.transformer["attention_dropout_rate"])

        self.softmax = Softmax(dim=-1)

    def transpose_for_scores(self, x):
        new_x_shape = x.size()[:-1] + (self.num_attention_heads, self.attention_head_size)
        x = x.view(*new_x_shape)
        return x.permute(0, 2, 1, 3)

    def forward(self, hidden_states, attnMask=None):
        # print("[INFO inside Attention-Forward] hidden_states:", hidden_states.shape) # 2, 510, 768
        mixed_query_layer = self.query(hidden_states) # 2, 510, 768
        mixed_key_layer = self.key(hidden_states) # 2, 510, 768
        mixed_value_layer = self.value(hidden_states) # 2, 510, 768
        # print("[INFO inside Attention-Forward] mixed_query_layer:", mixed_query_layer.shape)
        # print("[INFO inside Attention-Forward] mixed_key_layer:", mixed_key_layer.shape)
        # print("[INFO inside Attention-Forward] mixed_value_layer:", mixed_value_layer.shape)

        query_layer = self.transpose_for_scores(mixed_query_layer)
        key_layer = self.transpose_for_scores(mixed_key_layer)
        value_layer = self.transpose_for_scores(mixed_value_layer)

        # attnMask_T = self.transpose_for_scores(attnMask)
        # print("[INFO inside Attention-Forward] query_layer:", query_layer.shape)
        # print("[INFO inside Attention-Forward] key_layer:", key_layer.shape)
        # print("[INFO inside Attention-Forward] value_layer:", value_layer.shape)
        # print("[INFO inside Attention-Forward] attnMask_T:", attnMask_T.shape)

        attention_scores = torch.matmul(query_layer, key_layer.transpose(-1, -2))
        attention_scores = attention_scores / math.sqrt(self.attention_head_size)

        if attnMask is not None: # Nahid added 18th Oct 2022
            # print("[INFO inside Attention-Forward] attention_scores:", attention_scores.shape)
            # print("[INFO inside Attention-Forward] attnMask:", attnMask.shape)
            # torch.save(attention_scores, 'attention_scoresExp_before.pt') # 3, 12, 510, 510
            attention_scores = attention_scores + attnMask # 1001v for adding | 2001v for multiply
            # print(attention_scores.shape)
            # torch.save(attention_scores, 'attention_scoresExp_After.pt') # 3, 12, 510, 510
            # exit()

        attention_probs = self.softmax(attention_scores) # maybe multiply Mask after this line.
        # torch.save(attention_probs, 'attention_probExp_SoftMax.pt')
        # print("[INFO inside Attention-Forward] attention_probs:", attention_probs.shape)
        weights = attention_probs if self.vis else None
        attention_probs = self.attn_dropout(attention_probs)

        context_layer = torch.matmul(attention_probs, value_layer)
        context_layer = context_layer.permute(0, 2, 1, 3).contiguous()
        new_context_layer_shape = context_layer.size()[:-2] + (self.all_head_size,)
        context_layer = context_layer.view(*new_context_layer_shape)
        attention_output = self.out(context_layer)
        attention_output = self.proj_dropout(attention_output)       
        
        
        # if attnMask is not None: # Nahid added 18th Oct 2022
        #     return attention_output+attnMask[:,:,None], weights # https://github.com/huggingface/transformers/blob/main/src/transformers/modeling_utils.py
        # else:
        #     return attention_output, weights
        # print(attention_output.shape)
        # print(attention_output)
        # torch.save(attention_output, 'attention_outputExp.pt') # 3, 510, 768
        return attention_output, weights


class Mlp(nn.Module):
    def __init__(self, config):
        super(Mlp, self).__init__()
        self.fc1 = Linear(config.hidden_size, config.transformer["mlp_dim"])
        self.fc2 = Linear(config.transformer["mlp_dim"], config.hidden_size)
        self.act_fn = ACT2FN["gelu"]
        self.dropout = Dropout(config.transformer["dropout_rate"])

        self._init_weights()

    def _init_weights(self):
        nn.init.xavier_uniform_(self.fc1.weight)
        nn.init.xavier_uniform_(self.fc2.weight)
        nn.init.normal_(self.fc1.bias, std=1e-6)
        nn.init.normal_(self.fc2.bias, std=1e-6)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act_fn(x)
        x = self.dropout(x)
        x = self.fc2(x)
        x = self.dropout(x)
        return x


class Embeddings(nn.Module):
    """Construct the embeddings from patch, position embeddings.
    """
    def __init__(self, config, img_size, in_channels=3, embedding=False):
        super(Embeddings, self).__init__()
        self.hybrid = None
        self.embedding = embedding
        # img_size = _pair(img_size)

        # if config.patches.get("grid") is not None:
        #     grid_size = config.patches["grid"]
        #     patch_size = (img_size[0] // 16 // grid_size[0], img_size[1] // 16 // grid_size[1])
        #     n_patches = (img_size[0] // 16) * (img_size[1] // 16)
        #     self.hybrid = True
        # else:
        #     patch_size = _pair(config.patches["size"])
        #     #patch_size=_pair(8)
        #     n_patches = (img_size[0] // patch_size[0]) * (img_size[1] // patch_size[1])
        #     self.hybrid = False

        # if self.hybrid:
        #     self.hybrid_model = ResNetV2(block_units=config.resnet.num_layers,
        #                                  width_factor=config.resnet.width_factor)
        #     in_channels = self.hybrid_model.width * 16
        # self.patch_embeddings = Conv2d(in_channels=in_channels,
        #                                out_channels=config.hidden_size,
        #                                kernel_size=patch_size,
        #                                stride=patch_size)
        # self.position_embeddings = nn.Parameter(torch.zeros(1, 200+10, config.hidden_size)) # n_patches+1 = 192+1? || hidden_size = 768 || 192+10 or 200+10
        self.cls_token = nn.Parameter(torch.zeros(1, 10, config.hidden_size)) # 9 class Tokens? || was 1, 1, config.hidden_size

        self.dropout = Dropout(config.transformer["dropout_rate"])
        # print(patch_size, n_patches)
        # print(192) # added by Nahid

    def forward(self, x):
        B = x.shape[0]
        cls_tokens = self.cls_token.expand(B, -1, -1)

        if self.hybrid:
            x = self.hybrid_model(x)
        # x = self.patch_embeddings(x)
        # print("[INFO] inside Embeddings: X.shape")
        # x = x.flatten(2) # why 2?
        # x = x.transpose(-1, -2)
        x = torch.cat((cls_tokens, x), dim=1) # conc

        # embeddings = x + self.position_embeddings ## was active 3rd Oct 2022
        embeddings = x
        embeddings = self.dropout(embeddings)
        #return x
        if self.embedding:
            return embeddings
        else:
            return x


class Block(nn.Module):
    def __init__(self, config, vis):
        super(Block, self).__init__()
        self.hidden_size = config.hidden_size
        self.attention_norm = LayerNorm(config.hidden_size, eps=1e-6)
        self.ffn_norm = LayerNorm(config.hidden_size, eps=1e-6)
        self.ffn = Mlp(config)
        self.attn = Attention(config, vis)

    def forward(self, x, attnMask):
        h = x        
        x = self.attention_norm(x)        
        x, weights = self.attn(x, attnMask)
        # torch.save(h, 'hidden_states.pt')
        x = x + h

        h = x
        x = self.ffn_norm(x)
        x = self.ffn(x)
        x = x + h        
        return x, weights

    def load_from(self, weights, n_block):
        ROOT = f"Transformer/encoderblock_{n_block}"
        with torch.no_grad():
            query_weight = np2th(weights[pjoin(ROOT, ATTENTION_Q, "kernel")]).view(self.hidden_size, self.hidden_size).t()
            key_weight = np2th(weights[pjoin(ROOT, ATTENTION_K, "kernel")]).view(self.hidden_size, self.hidden_size).t()
            value_weight = np2th(weights[pjoin(ROOT, ATTENTION_V, "kernel")]).view(self.hidden_size, self.hidden_size).t()
            out_weight = np2th(weights[pjoin(ROOT, ATTENTION_OUT, "kernel")]).view(self.hidden_size, self.hidden_size).t()

            query_bias = np2th(weights[pjoin(ROOT, ATTENTION_Q, "bias")]).view(-1)
            key_bias = np2th(weights[pjoin(ROOT, ATTENTION_K, "bias")]).view(-1)
            value_bias = np2th(weights[pjoin(ROOT, ATTENTION_V, "bias")]).view(-1)
            out_bias = np2th(weights[pjoin(ROOT, ATTENTION_OUT, "bias")]).view(-1)

            self.attn.query.weight.copy_(query_weight)
            self.attn.key.weight.copy_(key_weight)
            self.attn.value.weight.copy_(value_weight)
            self.attn.out.weight.copy_(out_weight)
            self.attn.query.bias.copy_(query_bias)
            self.attn.key.bias.copy_(key_bias)
            self.attn.value.bias.copy_(value_bias)
            self.attn.out.bias.copy_(out_bias)

            mlp_weight_0 = np2th(weights[pjoin(ROOT, FC_0, "kernel")]).t()
            mlp_weight_1 = np2th(weights[pjoin(ROOT, FC_1, "kernel")]).t()
            mlp_bias_0 = np2th(weights[pjoin(ROOT, FC_0, "bias")]).t()
            mlp_bias_1 = np2th(weights[pjoin(ROOT, FC_1, "bias")]).t()

            self.ffn.fc1.weight.copy_(mlp_weight_0)
            self.ffn.fc2.weight.copy_(mlp_weight_1)
            self.ffn.fc1.bias.copy_(mlp_bias_0)
            self.ffn.fc2.bias.copy_(mlp_bias_1)

            self.attention_norm.weight.copy_(np2th(weights[pjoin(ROOT, ATTENTION_NORM, "scale")]))
            self.attention_norm.bias.copy_(np2th(weights[pjoin(ROOT, ATTENTION_NORM, "bias")]))
            self.ffn_norm.weight.copy_(np2th(weights[pjoin(ROOT, MLP_NORM, "scale")]))
            self.ffn_norm.bias.copy_(np2th(weights[pjoin(ROOT, MLP_NORM, "bias")]))


class Encoder(nn.Module):
    def __init__(self, config, vis):
        super(Encoder, self).__init__()
        self.vis = vis
        self.layer = nn.ModuleList()
        self.encoder_norm = LayerNorm(config.hidden_size, eps=1e-6)
        for _ in range(config.transformer["num_layers"]):
            layer = Block(config, vis)
            self.layer.append(copy.deepcopy(layer))

    def forward(self, hidden_states, attnMask=None):
        attn_weights = []
        for layer_block in self.layer:
            hidden_states, weights = layer_block(hidden_states, attnMask)
            # print("[INFO] inside Encoder_Class: hidden_states.shape", hidden_states.shape)
            if self.vis:
                attn_weights.append(weights)
        encoded = self.encoder_norm(hidden_states)
        return encoded, attn_weights


class Transformer(nn.Module):
    def __init__(self, config, img_size, vis, embedding=False):
        super(Transformer, self).__init__()
        self.embeddings = Embeddings(config, img_size=img_size, embedding=embedding)
        self.encoder = Encoder(config, vis)

    def forward(self, input_ids, attnMask): # x
        embedding_output = self.embeddings(input_ids) # was active        
        encoded, attn_weights = self.encoder(embedding_output, attnMask) # was embedding_output || now input_ids
        return encoded, attn_weights 


class VisionTransformer(nn.Module):
    def __init__(self, config, img_size=224, num_classes=21843, HS=6144, MD=1024, NH=2, NL=2, zero_head=False, vis=False, embedding=False, seqL=512): # seqL=numberOfSlicesPerPatient
        super(VisionTransformer, self).__init__()
        config.hidden_size = HS
        config.transformer["mlp_dim"] = MD
        config.transformer["num_heads"] = NH
        config.transformer["num_layers"] = NL 

        self.num_classes = num_classes
        num_classes = 1
        self.zero_head = zero_head
        self.classifier = config.classifier

        self.transformer = Transformer(config, img_size, vis, embedding)
        # self.head0 = Linear(config.hidden_size, seqL) # was 192
        self.head1 = Linear(config.hidden_size, num_classes) # 768 -> 1
        self.head2 = Linear(config.hidden_size, num_classes)
        self.head3 = Linear(config.hidden_size, num_classes)
        self.head4 = Linear(config.hidden_size, num_classes)
        self.head5 = Linear(config.hidden_size, num_classes)
        self.head6 = Linear(config.hidden_size, num_classes)
        self.head7 = Linear(config.hidden_size, num_classes)
        self.head8 = Linear(config.hidden_size, num_classes)
        self.head9 = Linear(config.hidden_size, num_classes)


    def forward(self, x, attnMask=None, labels=None):
        if attnMask is not None: # https://github.com/huggingface/transformers/blob/main/src/transformers/modeling_utils.py
            attnMask = torch.cat((torch.ones(attnMask.shape[0],10).cuda(), attnMask), dim=1).cuda() # conc-ing 10 headers at the front
            attnMask = attnMask[:, None, None, :]
            attnMask = attnMask.to(dtype=attnMask.dtype) # active if we add attention_mask
            attnMask = (1.0 - attnMask) * torch.finfo(attnMask.dtype).min # active if we add attention_mask

            # attnMask = attnMask.to(dtype=attnMask.dtype)  # fp16 compatibility
            # attnMask = (1.0 - attnMask) * torch.finfo(attnMask.dtype).min  ## converting all 1 to 0 and 0 to negative very minimum value
            # attnMask = (1.0 - attnMask) * (-1000)

        x, attn_weights = self.transformer(x, attnMask) # output X => B 510+10 768
        # print("[INFO] inside VisionTransformer: x.shape", x.shape)

        logits_restEverything = x[:, 10:] # B 192 768
        # print("[INFO] inside VisionTransformer: logits_restEverything.shape", logits_restEverything.shape)

        logits_clstoken = x[:, 0:10] # something is not right # B 1 768
        # print("[INFO] inside VisionTransformer: logits_clstoken.shape", logits_clstoken.shape)

        # logits = self.head(logits_restEverything) # > 768 class output
        # logits0 = self.head0(logits_clstoken[:,0])
        # print("[INFO] inside VisionTransformer: logits0.shape", logits0.shape)
        logits0 = logits_clstoken[:,0]

        # logits1 = logits_clstoken[:,1] # > 1 class output || later 0 -> 1 class output (exam-level)
        # logits2 = logits_clstoken[:,2]
        # logits3 = logits_clstoken[:,3]
        # logits4 = logits_clstoken[:,4]
        # logits5 = logits_clstoken[:,5]
        # logits6 = logits_clstoken[:,6]
        # logits7 = logits_clstoken[:,7]
        # logits8 = logits_clstoken[:,8]
        # logits9 = logits_clstoken[:,9]

        logits1 = self.head1(logits_clstoken[:,1]) # > 1 class output || later 0 -> 1 class output (exam-level)
        logits2 = self.head2(logits_clstoken[:,2])
        logits3 = self.head3(logits_clstoken[:,3])
        logits4 = self.head4(logits_clstoken[:,4])
        logits5 = self.head5(logits_clstoken[:,5])
        logits6 = self.head6(logits_clstoken[:,6])
        logits7 = self.head7(logits_clstoken[:,7])
        logits8 = self.head8(logits_clstoken[:,8])
        logits9 = self.head9(logits_clstoken[:,9])

        if labels is not None:
            loss_fct = CrossEntropyLoss()
            loss = loss_fct(logits.view(-1, self.num_classes), labels.view(-1))
            return loss
        else:
            # return logits, attn_weights # was active
            # print("[INFO] inside VisionTransformer: Return:", logits.shape, logits_restEverything.shape)
            return [logits0, logits1, logits2, logits3, logits4, logits5, logits6, logits7, logits8, logits9], logits_restEverything
            # return [logits1, logits2, logits3, logits4, logits5, logits6, logits7, logits8, logits9], logits_restEverything

    def load_from(self, weights):
        with torch.no_grad():
            if self.zero_head:
                nn.init.zeros_(self.head.weight)
                nn.init.zeros_(self.head.bias)
            else:
                self.head.weight.copy_(np2th(weights["head/kernel"]).t())
                self.head.bias.copy_(np2th(weights["head/bias"]).t())

            # self.transformer.embeddings.patch_embeddings.weight.copy_(np2th(weights["embedding/kernel"], conv=True)) # load weights for embeddings
            # self.transformer.embeddings.patch_embeddings.bias.copy_(np2th(weights["embedding/bias"])) # load weights for embeddings
            # self.transformer.embeddings.cls_token.copy_(np2th(weights["cls"])) # load weights for embeddings
            self.transformer.encoder.encoder_norm.weight.copy_(np2th(weights["Transformer/encoder_norm/scale"]))
            self.transformer.encoder.encoder_norm.bias.copy_(np2th(weights["Transformer/encoder_norm/bias"]))

            # posemb = np2th(weights["Transformer/posembed_input/pos_embedding"])
            # posemb_new = self.transformer.embeddings.position_embeddings # load weights for embeddings
            # if posemb.size() == posemb_new.size():
            #     # self.transformer.embeddings.position_embeddings.copy_(posemb) # load weights for embeddings
            #     check=1
            # else:
            #     logger.info("load_pretrained: resized variant: %s to %s" % (posemb.size(), posemb_new.size()))
            #     ntok_new = posemb_new.size(1)

            #     if self.classifier == "token":
            #         posemb_tok, posemb_grid = posemb[:, :1], posemb[0, 1:]
            #         ntok_new -= 1
            #     else:
            #         posemb_tok, posemb_grid = posemb[:, :0], posemb[0]

            #     gs_old = int(np.sqrt(len(posemb_grid)))
            #     gs_new = int(np.sqrt(ntok_new))
            #     print('load_pretrained: grid-size from %s to %s' % (gs_old, gs_new))
            #     posemb_grid = posemb_grid.reshape(gs_old, gs_old, -1)

            #     zoom = (gs_new / gs_old, gs_new / gs_old, 1)
            #     posemb_grid = ndimage.zoom(posemb_grid, zoom, order=1)
            #     posemb_grid = posemb_grid.reshape(1, gs_new * gs_new, -1)
            #     posemb = np.concatenate([posemb_tok, posemb_grid], axis=1)
                # self.transformer.embeddings.position_embeddings.copy_(np2th(posemb)) # load weights for embeddings

            for bname, block in self.transformer.encoder.named_children():
                for uname, unit in block.named_children():
                    unit.load_from(weights, n_block=uname)

            # if self.transformer.embeddings.hybrid: # load weights for embeddings
            #     self.transformer.embeddings.hybrid_model.root.conv.weight.copy_(np2th(weights["conv_root/kernel"], conv=True))
            #     gn_weight = np2th(weights["gn_root/scale"]).view(-1)
            #     gn_bias = np2th(weights["gn_root/bias"]).view(-1)
            #     self.transformer.embeddings.hybrid_model.root.gn.weight.copy_(gn_weight)
            #     self.transformer.embeddings.hybrid_model.root.gn.bias.copy_(gn_bias)

            #     for bname, block in self.transformer.embeddings.hybrid_model.body.named_children():
            #         for uname, unit in block.named_children():
            #             unit.load_from(weights, n_block=bname, n_unit=uname)


CONFIGS = {
    'ViT-B_16': configs.get_b16_config(),
    'ViT-B_16_NI': configs.get_b16_NI_config(),
    'ViT-B_32': configs.get_b32_config(),
    'ViT-L_16': configs.get_l16_config(),
    'ViT-L_32': configs.get_l32_config(),
    'ViT-H_14': configs.get_h14_config(),
    'R50-ViT-B_16': configs.get_r50_b16_config(),
    'testing': configs.get_testing(),
}
