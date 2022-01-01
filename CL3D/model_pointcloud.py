import torch
import torch.nn as nn
from model_shape import SDFNet


def maxpool(x, dim=-1, keepdim=False):
    out, _ = x.max(dim=dim, keepdim=keepdim)
    return out

class PointCloudNet(SDFNet):

	def __init__(self, config, input_point_dim=3, latent_dim=512, size_hidden=512, pretrained=False):
		super().__init__(config, input_point_dim, latent_dim, size_hidden, pretrained)
		self.encoder = ResnetPointnet(latent_dim=latent_dim, size_hidden=size_hidden)


class ResnetPointnet(nn.Module):
    ''' PointNet-based encoder network with ResNet blocks

    Args:
        latent_dim: dimension of conditioned code, default to 512
        point_dim: input points dimension, default to 3
        size_hidden: dimension of points block hidden size, default to 512
        pretrained: whether the encoder is ImageNet pretrained, 
            default to False
    '''

    def __init__(self, latent_dim=128, point_dim=3, size_hidden=128):
        super().__init__()
        self.latent_dim = latent_dim

        self.fc_pos = nn.Linear(point_dim, 2*size_hidden)
        self.block_0 = ResnetBlockFC(2*size_hidden, size_hidden)
        self.block_1 = ResnetBlockFC(2*size_hidden, size_hidden)
        self.block_2 = ResnetBlockFC(2*size_hidden, size_hidden)
        self.block_3 = ResnetBlockFC(2*size_hidden, size_hidden)
        self.block_4 = ResnetBlockFC(2*size_hidden, size_hidden)
        self.fc_c = nn.Linear(size_hidden, latent_dim)

        self.actvn = nn.ReLU()
        self.pool = maxpool

    def forward(self, p):
        _, T, D = p.size()

        # output size: B x T X F
        net = self.fc_pos(p)
        net = self.block_0(net)
        pooled = self.pool(net, dim=1, keepdim=True).expand(net.size())
        net = torch.cat([net, pooled], dim=2)

        net = self.block_1(net)
        pooled = self.pool(net, dim=1, keepdim=True).expand(net.size())
        net = torch.cat([net, pooled], dim=2)

        net = self.block_2(net)
        pooled = self.pool(net, dim=1, keepdim=True).expand(net.size())
        net = torch.cat([net, pooled], dim=2)

        net = self.block_3(net)
        pooled = self.pool(net, dim=1, keepdim=True).expand(net.size())
        net = torch.cat([net, pooled], dim=2)

        net = self.block_4(net)

        # Recude to  B x F
        net = self.pool(net, dim=1)

        c = self.fc_c(self.actvn(net))

        return c

class ResnetBlockFC(nn.Module):
    ''' Fully connected ResNet Block class.
    Args:
        size_in (int): input dimension
        size_out (int): output dimension
        size_h (int): hidden dimension
    '''

    def __init__(self, size_in, size_out=None, size_h=None):
        super().__init__()
        # Attributes
        if size_out is None:
            size_out = size_in

        if size_h is None:
            size_h = min(size_in, size_out)

        self.size_in = size_in
        self.size_h = size_h
        self.size_out = size_out
        # Submodules
        self.fc_0 = nn.Linear(size_in, size_h)
        self.fc_1 = nn.Linear(size_h, size_out)
        self.actvn = nn.ReLU()

        if size_in == size_out:
            self.shortcut = None
        else:
            self.shortcut = nn.Linear(size_in, size_out, bias=False)
        # Initialization
        nn.init.zeros_(self.fc_1.weight)

    def forward(self, x):
        net = self.fc_0(self.actvn(x))
        dx = self.fc_1(self.actvn(net))

        if self.shortcut is not None:
            x_s = self.shortcut(x)
        else:
            x_s = x

        return x_s + dx
