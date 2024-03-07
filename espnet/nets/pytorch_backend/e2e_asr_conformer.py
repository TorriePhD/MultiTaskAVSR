# Copyright 2019 Shigeki Karita
#  Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)

"""Transformer speech recognition model (pytorch)."""

import logging
import numpy
import torch
from espnet.nets.pytorch_backend.mamba.mixer_seq_simple import MambaLMHeadModel
from mamba_ssm.models.config_mamba import MambaConfig
from espnet.nets.pytorch_backend.backbones.conv1d_extractor import Conv1dResNet
from espnet.nets.pytorch_backend.backbones.conv3d_extractor import Conv3dResNet
from espnet.nets.pytorch_backend.ctc import CTC
from espnet.nets.pytorch_backend.nets_utils import (
    make_non_pad_mask,
    th_accuracy,
)
from espnet.nets.pytorch_backend.transformer.add_sos_eos import add_sos_eos
from espnet.nets.pytorch_backend.transformer.decoder import Decoder
from espnet.nets.pytorch_backend.transformer.encoder import Encoder
from espnet.nets.pytorch_backend.transformer.positionwise_feed_forward import PositionwiseFeedForward
from espnet.nets.pytorch_backend.transformer.label_smoothing_loss import LabelSmoothingLoss
from espnet.nets.pytorch_backend.transformer.mask import target_mask
from espnet.nets.pytorch_backend.nets_utils import MLPHead


class E2E(torch.nn.Module):
    def __init__(self, odim, args, ignore_id=-1):
        torch.nn.Module.__init__(self)
        # if args is a dictionary create two encoders, one for audio and one for video
        self.crossmodal = False
        self.blank = 0
        self.sos = odim - 1
        self.eos = odim - 1
        self.odim = odim
        self.ignore_id = ignore_id
        self.adim = args["visual_backbone"].adim
        if isinstance(args, dict):
            self.crossmodal = True
            self.audioFrontEnd = Conv1dResNet(relu_type = args["audio_backbone"].relu_type ,a_upsample_ratio = args["audio_backbone"].a_upsample_ratio)
            self.videoFrontEnd = Conv3dResNet(relu_type = args["visual_backbone"].relu_type)
            self.videoEmbed = torch.nn.Linear(512, self.adim)
            self.audioEmbed = torch.nn.Linear(512, self.adim)
            self.mambaconfig = MambaConfig(d_model=self.adim,n_layer=args["mamba"].n_layer,vocab_size=self.odim,pad_vocab_size_multiple=4)
            self.mamba = MambaLMHeadModel(self.mambaconfig,device="cuda",dtype=torch.float32).cuda()
            self.transformer_input_layer = {}
            self.transformer_input_layer["audio"] = args["audio_backbone"].transformer_input_layer
            self.transformer_input_layer["video"] = args["visual_backbone"].transformer_input_layer
            self.a_upsample_ratio = {}
            self.a_upsample_ratio["audio"] = args["audio_backbone"].a_upsample_ratio
            self.a_upsample_ratio["video"] = args["visual_backbone"].a_upsample_ratio

            self.criterion = LabelSmoothingLoss(
                self.odim,
                self.ignore_id,
                args["visual_backbone"].lsm_weight,
                args["visual_backbone"].transformer_length_normalized_loss,
            )
            self.mtlalpha = args["visual_backbone"].mtlalpha
            # if args["visual_backbone"].mtlalpha > 0.0:
            #     self.ctc = CTC(
            #         odim, self.odim, args["visual_backbone"].dropout_rate, ctc_type=args["visual_backbone"].ctc_type, reduce=True
            #     )
            # else:
            #     self.ctc = None
                
        else:
            self.encoder = self.createEncoder(args)
            self.transformer_input_layer = args.transformer_input_layer
            self.a_upsample_ratio = args.a_upsample_ratio

            self.proj_decoder = None
            if args.adim != args.ddim:
                self.proj_decoder = torch.nn.Linear(args.adim, args.ddim)

            if args.mtlalpha < 1:
                self.decoder = Decoder(
                    odim=odim,
                    attention_dim=args.ddim,
                    attention_heads=args.dheads,
                    linear_units=args.dunits,
                    num_blocks=args.dlayers,
                    dropout_rate=args.dropout_rate,
                    positional_dropout_rate=args.dropout_rate,
                    self_attention_dropout_rate=args.transformer_attn_dropout_rate,
                    src_attention_dropout_rate=args.transformer_attn_dropout_rate,
                )
            else:
                self.decoder = None
            # self.lsm_weight = a
            self.criterion = LabelSmoothingLoss(
                self.odim,
                self.ignore_id,
                args.lsm_weight,
                args.transformer_length_normalized_loss,
            )

            self.adim = args.adim
            self.mtlalpha = args.mtlalpha
            if args.mtlalpha > 0.0:
                self.ctc = CTC(
                    odim, args.adim, args.dropout_rate, ctc_type=args.ctc_type, reduce=True
                )
            else:
                self.ctc = None
        

        
    def createEncoder(self, args):
        return Encoder(
                attention_dim=args.adim,
                attention_heads=args.aheads,
                linear_units=args.eunits,
                num_blocks=args.elayers,
                input_layer=args.transformer_input_layer,
                dropout_rate=args.dropout_rate,
                positional_dropout_rate=args.dropout_rate,
                attention_dropout_rate=args.transformer_attn_dropout_rate,
                encoder_attn_layer_type=args.transformer_encoder_attn_layer_type,
                macaron_style=args.macaron_style,
                use_cnn_module=args.use_cnn_module,
                cnn_module_kernel=args.cnn_module_kernel,
                zero_triu=getattr(args, "zero_triu", False),
                a_upsample_ratio=args.a_upsample_ratio,
                relu_type=getattr(args, "relu_type", "swish"),
            )

    def forward(self, x, lengths, label):
        if self.crossmodal:
            return self.forward_crossmodal(x, lengths, label)
        if self.transformer_input_layer == "conv1d":
            lengths = torch.div(lengths, 640, rounding_mode="trunc")
        padding_mask = make_non_pad_mask(lengths).to(x.device).unsqueeze(-2)

        x, _ = self.encoder(x, padding_mask)

        # ctc loss
        loss_ctc, ys_hat = self.ctc(x, lengths, label)

        if self.proj_decoder:
            x = self.proj_decoder(x)

        # decoder loss
        ys_in_pad, ys_out_pad = add_sos_eos(label, self.sos, self.eos, self.ignore_id)
        ys_mask = target_mask(ys_in_pad, self.ignore_id)
        pred_pad, _ = self.decoder(ys_in_pad, ys_mask, x, padding_mask)
        loss_att = self.criterion(pred_pad, ys_out_pad)
        loss = self.mtlalpha * loss_ctc + (1 - self.mtlalpha) * loss_att

        acc = th_accuracy(
            pred_pad.view(-1, self.odim), ys_out_pad, ignore_label=self.ignore_id
        )

        return loss, loss_ctc, loss_att, acc
    def getAudioFeatures(self, audio, vidSize,padding_mask=None,):
        if len(audio.size()) == 2:
            audio = audio.unsqueeze(0)
        xAudio = self.audioFrontEnd(audio)
        xAudio = self.audioEmbed(xAudio)
        xAudio = self.mamba(xAudio)
        size = vidSize
        #padd xAud to match size of xVid
        xAudio = torch.nn.functional.pad(xAudio, (0, 0, 0, size - xAudio.size(1)), "constant")
        return xAudio
    def getVideoFeatures(self, video, padding_mask=None):
        xVideo = self.videoFrontEnd(video)
        xVideo = self.videoEmbed(xVideo)
        xVideo = self.mamba(xVideo)
        # xVideo,_ = self.videoEncoder(video, padding_mask)
        return xVideo
    # def getCombinedFeatures(self, xVideo, xAudio):
    #     x_combined = torch.cat((xVideo, xAudio), dim=2)
    #     x_combined = self.fusion(x_combined)
    #     return x_combined
    def getSingleModalFeatures(self, video, audio, modality, padding_mask,vidSize=None,):
        if modality == "video":
            myPaddingMask = None
            if padding_mask is not None:
                myPaddingMask = padding_mask["video"]
            xVid = self.getVideoFeatures(video, myPaddingMask)
            return xVid
        elif modality == "audio":
            myPaddingMask = None
            if padding_mask is not None:
                myPaddingMask = padding_mask["audio"]

            xAud = self.getAudioFeatures(audio, vidSize, myPaddingMask)
            return xAud
        # else:
        #     if padding_mask is None:
        #         padding_mask = {}
        #         padding_mask["video"] = None
        #         padding_mask["audio"] = None
        #     xVid = self.getVideoFeatures(video, padding_mask["video"])
        #     xAud = self.getAudioFeatures(audio, video.size(1), padding_mask["audio"])
        #     x_combined = self.getCombinedFeatures(xVid, xAud)
        #     return x_combined
    def getModalities(self, x):
        modality = torch.zeros(x['video'].size(0), dtype=torch.long, device=x["video"].device)
        # Determine modality by where video and audio are present (all zeros if not present)
        #check if video is all zeros
        whereVideo = x['video'].sum(dim=(1,2,3,4)) != 0
        whereAudio = x['audio'].sum(dim=(1,2)) != 0
        modality[whereVideo] = 0
        modality[whereAudio] = 1
        modality[whereVideo & whereAudio] = 2
        return modality
    def getAllModalFeatures(self,x,lengths=None,label=None):
        if lengths is not None:
            padding_mask = {}
            for key in lengths.keys():
                myLengths = lengths[key]
                if key == "audio":
                    myLengths = torch.div(lengths[key], 640, rounding_mode="trunc")
                padding_mask[key] = make_non_pad_mask(myLengths).to(x[key].device).unsqueeze(-2)
        else:
            padding_mask = None
        vidSize = x["video"].size(1)
        # modalities = self.getModalities(x)
        enc_feat = torch.zeros(x['video'].size(0)*2, x['video'].size(1), self.odim, device=x["video"].device)
        modalities = torch.cat((torch.zeros(x['video'].size(0), dtype=torch.long, device=x["video"].device),
                                torch.ones(x['video'].size(0), dtype=torch.long, device=x["video"].device)))

        for modality in ["audio", "video"]:
            if modality == "audio":
                indexes = modalities == 1
                video = None
                audio = x["audio"].clone()
            else:
                indexes = modalities == 0
                video = x["video"].clone()
                audio = None
            enc_feat[indexes] = self.getSingleModalFeatures(video, audio, modality, padding_mask, vidSize, )
        # ctc loss
        #repeat label 3 times
        if label is not None:
            label = torch.cat((label, label), dim=0)
        if lengths is not None:
            lengths["video"] = torch.cat((lengths["video"], lengths["video"]), dim=0)
            lengths["audio"] = torch.cat((lengths["audio"], lengths["audio"]), dim=0)
            padding_mask["video"] = torch.cat((padding_mask["video"], padding_mask["video"]), dim=0)
            padding_mask["audio"] = torch.cat((padding_mask["audio"], padding_mask["audio"]), dim=0)

        return enc_feat, lengths, padding_mask, label, modalities
    def forward_crossmodal(self, x, lengths, label):
        enc_feat, lengths, padding_mask, label, modalities = self.getAllModalFeatures(x,lengths,label)
        # loss_ctcMod, ys_hat = self.ctc(enc_feat, lengths["video"], label)
        # loss_ctc = loss_ctcMod
        
        # decoder loss
        ys_in_pad, ys_out_pad = add_sos_eos(label, self.sos, self.eos, self.ignore_id)
        ys_mask = target_mask(ys_in_pad, self.ignore_id)
        # if self.mtlalpha < 1:
        #     pred_pad, _ = self.decoder(ys_in_pad, ys_mask, enc_feat, padding_mask["video"])
        # else:
        #     pred_pad = None
        #cut off the second dimention of the enc_feat tensor to match the length of the ys_out_pad tensor second dimention get the last values, not the first
        enc_feat = enc_feat[:,enc_feat.size(1)-ys_out_pad.size(1):,:].contiguous() # TODO This is probably not the best thing to do. We should make mamba output the same size as the ys_out_pad tensor. 
        loss_att = self.criterion(enc_feat, ys_out_pad)
        loss = loss_att

        accAll = th_accuracy(
            enc_feat.view(-1, self.odim), ys_out_pad, ignore_label=self.ignore_id
        )
        acc = {}
        acc["video"] = th_accuracy(
            enc_feat[modalities==0].view(-1, self.odim), ys_out_pad[modalities==0], ignore_label=self.ignore_id
        )
        acc["audio"] = th_accuracy(
            enc_feat[modalities==1].view(-1, self.odim), ys_out_pad[modalities==1], ignore_label=self.ignore_id
        )
        return loss, loss, loss_att, accAll, acc
