'''Pre-activation ResNet in PyTorch.

Reference:
[1] Kaiming He, Xiangyu Zhang, Shaoqing Ren, Jian Sun
    Identity Mappings in Deep Residual Networks. arXiv:1603.05027
'''
import torch
import torch.nn as nn
import torch.nn.functional as F
import math

__all__ = ['RENSEResNet50']

class ShareGroupConv(nn.Module):
    def __init__(self,in_channel, out_channel, kernel_size, stride=1, padding=0, dialation=1,bias=False):
        super(ShareGroupConv, self).__init__()
        self.in_channel=in_channel
        self.out_channel = out_channel
        self.oralconv = nn.Conv2d(in_channel//out_channel, 1, kernel_size=kernel_size, stride=stride,
                              padding=padding, dilation=dialation, groups=1, bias=bias)

    def forward(self, x):
        out = None
        for j in range(0, self.out_channel):
            term = x[:,torch.arange(j,self.in_channel,step=self.out_channel),:,:]
            out_term = self.oralconv(term)

            out = torch.cat((out,out_term),dim=1) if not out is None else out_term
        return out


class RENSELayer(nn.Module):
    def __init__(self, in_channel,channel, reduction=16):
        super(RENSELayer, self).__init__()
        self.avg_pool1 = nn.AdaptiveAvgPool2d(1)
        self.avg_pool2 = nn.AdaptiveAvgPool2d(2)
        self.avg_pool4 = nn.AdaptiveAvgPool2d(4)
        self.sharegroupconv = ShareGroupConv(channel*3,channel,kernel_size=3,padding=0)
        self.sharegroupconv2 = ShareGroupConv(channel, channel, kernel_size=3, padding=1,stride=3)
        self.fc = nn.Sequential(
            nn.Linear(channel, channel // reduction, bias=False),
            nn.ReLU(inplace=True),
            nn.Linear(channel // reduction, channel, bias=False),
        )
        if in_channel != channel:
            self.att_fc = nn.Linear(in_channel,channel,bias=False)

    def forward(self, x):
        b, c, _, _ = x[0].size()
        y1=self.avg_pool1(x[0])
        y1=nn.functional.interpolate(y1,scale_factor=4,mode='nearest')
        y2=self.avg_pool2(x[0])
        y2 = nn.functional.interpolate(y2, scale_factor=2, mode='nearest')
        y4 = self.avg_pool4(x[0])
        y = torch.cat((y1,y2,y4),dim=1)
        y = self.sharegroupconv(y)
        y = self.sharegroupconv2(y).view(b, c)
        if x[1] is None:
            all_att = self.fc(y)
        else:
            pre_att = self.att_fc(x[1]) if hasattr(self, 'att_fc') else x[1]
            all_att = self.fc(y) + pre_att
        y = torch.sigmoid(all_att).view(b, c, 1, 1)
        return {0: x[0] * y.expand_as(x[0]), 1: all_att}


class PreActBlock(nn.Module):
    '''Pre-activation version of the BasicBlock.'''
    expansion = 1

    def __init__(self, in_planes, planes, stride=1):
        super(PreActBlock, self).__init__()
        self.bn1 = nn.BatchNorm2d(in_planes)
        self.conv1 = nn.Conv2d(in_planes, planes, kernel_size=3, stride=stride, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(planes)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, stride=1, padding=1, bias=False)
        self.se = RENSELayer(in_channel=in_planes, channel=planes*self.expansion)

        if stride != 1 or in_planes != self.expansion*planes:
            self.shortcut = nn.Sequential(
                nn.Conv2d(in_planes, self.expansion*planes, kernel_size=1, stride=stride, bias=False)
            )

    def forward(self, x):
        out = F.relu(self.bn1(x[0]))
        shortcut = self.shortcut(out) if hasattr(self, 'shortcut') else x[0]
        out = self.conv1(out)
        out = self.conv2(F.relu(self.bn2(out)))
        # Add SE block
        out = self.se({0:out,1:x[1]})
        out_x = out[0] + shortcut
        out_att = out[1]
        return {0: out_x,1:out_att}


class PreActBottleneck(nn.Module):
    '''Pre-activation version of the original Bottleneck module.'''
    expansion = 4

    def __init__(self, in_planes, planes, stride=1):
        super(PreActBottleneck, self).__init__()
        self.bn1 = nn.BatchNorm2d(in_planes)
        self.conv1 = nn.Conv2d(in_planes, planes, kernel_size=1, bias=False)
        self.bn2 = nn.BatchNorm2d(planes)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, stride=stride, padding=1, bias=False)
        self.bn3 = nn.BatchNorm2d(planes)
        self.conv3 = nn.Conv2d(planes, self.expansion*planes, kernel_size=1, bias=False)
        self.se = RENSELayer(in_channel=in_planes, channel=planes*self.expansion)

        if stride != 1 or in_planes != self.expansion*planes:
            self.shortcut = nn.Sequential(
                nn.Conv2d(in_planes, self.expansion*planes, kernel_size=1, stride=stride, bias=False)
            )

    def forward(self, x):
        out = F.relu(self.bn1(x[0]))
        shortcut = self.shortcut(out) if hasattr(self, 'shortcut') else x[0]
        out = self.conv1(out)
        out = self.conv2(F.relu(self.bn2(out)))
        out = self.conv3(F.relu(self.bn3(out)))
        # Add SE block
        out = self.se({0:out,1:x[1]})
        out_x = out[0] + shortcut
        out_att = out[1]
        return {0: out_x, 1: out_att}


class PreActResNet(nn.Module):
    def __init__(self, block, num_blocks, num_classes=1000,init_weights=True):
        super(PreActResNet, self).__init__()
        self.in_planes = 64

        self.conv1 = nn.Conv2d(3, 64, kernel_size=3, stride=1, padding=1, bias=False)
        self.layer1 = self._make_layer(block, 64, num_blocks[0], stride=1)
        self.layer2 = self._make_layer(block, 128, num_blocks[1], stride=2)
        self.layer3 = self._make_layer(block, 256, num_blocks[2], stride=2)
        self.layer4 = self._make_layer(block, 512, num_blocks[3], stride=2)
        self.linear = nn.Linear(512*block.expansion, num_classes)
        if init_weights:
            self._initialize_weights()

    def _make_layer(self, block, planes, num_blocks, stride):
        strides = [stride] + [1]*(num_blocks-1)
        layers = []
        for stride in strides:
            layers.append(block(self.in_planes, planes, stride))
            self.in_planes = planes * block.expansion
        return nn.Sequential(*layers)

    def _initialize_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, math.sqrt(2. / n))
                if m.bias is not None:
                    m.bias.data.zero_()
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()
            elif isinstance(m, nn.Linear):
                m.weight.data.normal_(0, 0.01)
                if hasattr(m, 'bias.data'):
                    m.bias.data.zero_()

    def forward(self, x):
        out = self.conv1(x)
        att = None
        out = {0: out, 1: att}
        out = self.layer1(out)
        out = self.layer2(out)
        out = self.layer3(out)
        out = self.layer4(out)
        out = F.avg_pool2d(out[0], 4)
        out = out.view(out.size(0), -1)
        out = self.linear(out)
        return out


def RENSEResNet18(num_classes=1000):
    return PreActResNet(PreActBlock, [2,2,2,2],num_classes)

def RENSEResNet34(num_classes=1000):
    return PreActResNet(PreActBlock, [3,4,6,3],num_classes)

def RENSEResNet50(num_classes=1000):
    return PreActResNet(PreActBottleneck, [3,4,6,3],num_classes)

def RENSEResNet101(num_classes=1000):
    return PreActResNet(PreActBottleneck, [3,4,23,3],num_classes)

def RENSEResNet152(num_classes=1000):
    return PreActResNet(PreActBottleneck, [3,8,36,3],num_classes)


def test():
    net = RENSEResNet50(num_classes=100)
    y = net((torch.randn(1,3,32,32)))
    print(y.size())

# test()
