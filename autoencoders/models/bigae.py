import torch
import torch.nn as nn
import torchvision
from torchvision import models

from edflow.util import retrieve

from autoencoders.models.util import ActNorm
from autoencoders.distributions import DiagonalGaussianDistribution
from autoencoders.models.biggan import load_variable_latsize_generator
from autoencoders.ckpt_util import get_ckpt_path


class ClassUp(nn.Module):
    def __init__(self, dim, depth, hidden_dim=256, use_sigmoid=False, out_dim=None):
        super().__init__()
        layers = []
        layers.append(nn.Linear(dim, hidden_dim))
        layers.append(nn.LeakyReLU())
        for d in range(depth):
            layers.append(nn.Linear(hidden_dim, hidden_dim))
            layers.append(nn.LeakyReLU())
        layers.append(nn.Linear(hidden_dim, dim if out_dim is None else out_dim))
        if use_sigmoid:
            layers.append(nn.Sigmoid())
        self.main = nn.Sequential(*layers)

    def forward(self, x):
        x = self.main(x.squeeze(-1).squeeze(-1))
        x = torch.nn.functional.softmax(x, dim=1)
        return x


class BigGANDecoderWrapper(nn.Module):
    """Wraps a BigGAN into our autoencoding framework"""
    def __init__(self, config):
        super().__init__()
        z_dim = retrieve(config, "Model/z_dim")
        image_size = retrieve(config, 'Model/in_size', default=128)
        use_actnorm = retrieve(config, 'Model/use_actnorm_in_dec', default=False)
        pretrained = retrieve(config, 'Model/pretrained', default=True)
        class_embedding_dim = 1000

        self.map_to_class_embedding = ClassUp(z_dim, depth=2, hidden_dim=2*class_embedding_dim,
                                              use_sigmoid=False, out_dim=class_embedding_dim)
        self.decoder = load_variable_latsize_generator(image_size, z_dim,
                                                       pretrained=pretrained,
                                                       use_actnorm=use_actnorm,
                                                       n_class=class_embedding_dim)

    def forward(self, x, labels=None):
        emb = self.map_to_class_embedding(x)
        x = self.decoder(x, emb)
        return x


class DenseEncoderLayer(nn.Module):
    def __init__(self, scale, spatial_size, out_size, in_channels=None,
                 width_multiplier=1):
        super().__init__()
        self.scale = scale
        self.wm = width_multiplier
        self.in_channels = int(self.wm*64*min(2**(self.scale-1), 16))
        if in_channels is not None:
            self.in_channels = in_channels
        self.out_channels = out_size
        self.kernel_size = spatial_size
        self.build()

    def forward(self, input):
        x = input
        for layer in self.sub_layers:
            x = layer(x)
        return x

    def build(self):
        self.sub_layers = nn.ModuleList([
                nn.Conv2d(
                    in_channels=self.in_channels,
                    out_channels=self.out_channels,
                    kernel_size=self.kernel_size,
                    stride=1,
                    padding=0,
                    bias=True)])


_norm_options = {
        "in": nn.InstanceNorm2d,
        "bn": nn.BatchNorm2d,
        "an": ActNorm}

rescale = lambda x: 0.5*(x+1)

class ResnetEncoder(nn.Module):
    def __init__(self, config):
        super().__init__()
        __possible_resnets = {
            'resnet18': models.resnet18,
            'resnet34': models.resnet34,
            'resnet50': models.resnet50,
            'resnet101': models.resnet101
        }
        self.config = config
        z_dim = retrieve(config, "Model/z_dim")
        ipt_size = retrieve(config, "Model/in_size")
        type_ = retrieve(config, "Model/type", default='resnet50')
        load_pretrained = retrieve(config, "Model/pretrained")
        norm_layer = _norm_options[retrieve(config, "Model/norm")]
        self.type = type_
        self.z_dim = z_dim
        self.model = __possible_resnets[type_](pretrained=load_pretrained, norm_layer=norm_layer)

        normalize = torchvision.transforms.Normalize(mean=self.mean, std=self.std)
        self.image_transform = torchvision.transforms.Compose(
                [torchvision.transforms.Lambda(lambda image: torch.stack([normalize(rescale(x)) for x in image]))]
                )

        size_pre_fc = self._get_spatial_size(ipt_size)
        assert size_pre_fc[2]==size_pre_fc[3], 'Output spatial size is not quadratic'
        spatial_size = size_pre_fc[2]
        num_channels_pre_fc = size_pre_fc[1]
        # replace last fc
        self.model.fc = DenseEncoderLayer(0,
                                          spatial_size=spatial_size,
                                          out_size=2*z_dim,
                                          in_channels=num_channels_pre_fc)

    def forward(self, x):
        x = self._pre_process(x)
        features = self.features(x)
        encoding = self.model.fc(features)
        return encoding

    def features(self, x):
        x = self._pre_process(x)
        x = self.model.conv1(x)
        x = self.model.bn1(x)
        x = self.model.relu(x)
        x = self.model.maxpool(x)
        x = self.model.layer1(x)
        x = self.model.layer2(x)
        x = self.model.layer3(x)
        x = self.model.layer4(x)
        x = self.model.avgpool(x)
        return x

    def post_features(self, x):
        x = self.model.fc(x)
        return x

    def _pre_process(self, x):
        x = self.image_transform(x)
        return x

    def _get_spatial_size(self, ipt_size):
        x = torch.randn(1, 3, ipt_size, ipt_size)
        return self.features(x).size()

    @property
    def mean(self):
        return [0.485, 0.456, 0.406]

    @property
    def std(self):
        return [0.229, 0.224, 0.225]

    @property
    def input_size(self):
        return [3, 224, 224]


class BigAE(nn.Module):
    def __init__(self, config):
        super().__init__()
        import torch.backends.cudnn as cudnn
        cudnn.benchmark = True
        self.be_deterministic = retrieve(config, "Model/deterministic",
                                         default=False)
        self.encoder = ResnetEncoder(config)
        self.decoder = BigGANDecoderWrapper(config=config)

    @classmethod
    def from_pretrained(cls, name):
        config_dict = {
            "animals": {
                "Model": {
                    "deterministic": False,
                    "in_size": 128,
                    "norm": "an",
                    "pretrained": False,
                    "type": "resnet101",
                    "use_actnorm_in_dec": True,
                    "z_dim": 128,
                }
            },
            "animalfaces": {
                "Model": {
                    "deterministic": False,
                    "in_size": 128,
                    "norm": "bn",
                    "pretrained": False,
                    "type": "resnet101",
                    "use_actnorm_in_dec": False,
                    "z_dim": 128,
                }
            },
        }
        ckpt_dict = {
            "animals": "bigae_animals",
            "animalfaces": "bigae_animalfaces",
        }

        if not name in config_dict:
            raise NotImplementedError(name)

        model = cls(config_dict[name])
        ckpt = get_ckpt_path(ckpt_dict[name])
        model.load_state_dict(torch.load(ckpt, map_location=torch.device("cpu")))
        model.eval()
        return model

    def encode(self, input):
        h = input
        h = self.encoder(h)
        return DiagonalGaussianDistribution(h, deterministic=self.be_deterministic)

    def decode(self, input):
        h = input
        h = self.decoder(h.squeeze(-1).squeeze(-1))
        return h

    def get_last_layer(self):
        return getattr(self.decoder.decoder.colorize.module, 'weight_bar')


if __name__ == "__main__":
    # > python autoencoders/models/bigae.py input.png output.png
    # writes the reconstruction of input.png to output.png
    import sys
    import numpy as np
    from PIL import Image
    import torch

    m = BigAE.from_pretrained("animals")
    if len(sys.argv) > 1:
        print("Loading {}".format(sys.argv[1]))
        I = Image.open(sys.argv[1])
        xin = torch.tensor(np.array(I)/127.5-1.0)
        xin = xin[None,...].transpose(3,2).transpose(2,1).float()

        p = m.encode(xin)
        xout = m.decode(p.mode())

        def save(x, path):
            x = x.transpose(0,1).transpose(1,2)
            x = x.detach().cpu().numpy()
            Image.fromarray(((x+1.0)*127.5).astype(np.uint8)).save(path)
        outpath = "xout.png"
        if len(sys.argv) > 2:
            outpath = sys.argv[2]
        save(xout[0], outpath)
