import torch
import torch.nn as nn

MAX_BATCH_SIZE = 64


def get_padding(kernel_size, stride=1, dilation=1, mode="centered"):
    """
    Computes 'same' padding given a kernel size, stride an dilation.

    Parameters
    ----------

    kernel_size: int
        kernel_size of the convolution

    stride: int
        stride of the convolution

    dilation: int
        dilation of the convolution

    mode: str
        either "centered", "causal" or "anticausal"
    """
    if kernel_size == 1: return (0, 0)
    p = (kernel_size - 1) * dilation + 1 - (stride-1)
    if mode == "centered":
        p_right = p // 2
        p_left = (p - 1) // 2
    elif mode == "causal":
        p_right = 0
        p_left = p // 2 + (p - 1) // 2
    elif mode == "anticausal":
        p_right = p // 2 + (p - 1) // 2
        p_left = 0
    else:
        raise Exception(f"Padding mode {mode} is not valid")
    return (max(0,p_left), max(0,p_right))


class CachedSequential(nn.Sequential):
    """
    Sequential operations with future compensation tracking
    """

    def __init__(self, *args, **kwargs):
        cumulative_delay = kwargs.pop("cumulative_delay", 0)
        stride = kwargs.pop("stride", 1)
        super().__init__(*args, **kwargs)

        self.cumulative_delay = int(cumulative_delay) * stride

        last_delay = 0
        for i in range(1, len(self) + 1):
            try:
                last_delay = self[-i].cumulative_delay
                break
            except AttributeError:
                pass
        self.cumulative_delay += last_delay


class Sequential(CachedSequential):
    pass


class CachedPadding1d(nn.Module):
    """
    Cached Padding implementation, replace zero padding with the end of
    the previous tensor.
    """

    def __init__(self, padding, crop=False):
        super().__init__()
        self.initialized = 0
        self.padding = padding
        self.crop = crop

    @torch.jit.unused
    @torch.no_grad()
    def init_cache(self, x):
        b, c, _ = x.shape
        self.register_buffer(
            "pad",
            torch.zeros(MAX_BATCH_SIZE, c, self.padding).to(x))
        self.initialized += 1

    def forward(self, x):
        if not self.initialized:
            self.init_cache(x)

        if self.padding:
            x = torch.cat([self.pad[:x.shape[0]], x], -1)
            self.pad[:x.shape[0]].copy_(x[..., -self.padding:])

            if self.crop:
                x = x[..., :-self.padding]

        return x


class CachedConv1d(nn.Conv1d):
    """
    Implementation of a Conv1d operation with cached padding
    """

    def __init__(self, *args, **kwargs):
        padding = kwargs.get("padding", 0)
        cumulative_delay = kwargs.pop("cumulative_delay", 0)

        kwargs["padding"] = 0

        super().__init__(*args, **kwargs)

        if isinstance(padding, int):
            r_pad = padding
            padding = 2 * padding
        elif isinstance(padding, list) or isinstance(padding, tuple):
            r_pad = padding[1]
            padding = padding[0] + padding[1]

        s = self.stride[0]
        cd = cumulative_delay

        stride_delay = (s - ((r_pad + cd) % s)) % s

        self.cumulative_delay = (r_pad + stride_delay + cd) // s

        self.cache = CachedPadding1d(padding)
        self.downsampling_delay = CachedPadding1d(stride_delay, crop=True)

    def forward(self, x):
        x = self.downsampling_delay(x)
        x = self.cache(x)
        return nn.functional.conv1d(
            x,
            self.weight,
            self.bias,
            self.stride,
            self.padding,
            self.dilation,
            self.groups,
        )


class CausalConvTranspose1d(torch.nn.ConvTranspose1d):
    """
    Transposed Conv with asymmetrical padding.
    """
    def __init__(self, in_channels, out_channels, kernel_size, stride, **kw):
        kw.pop('padding', 0)
        if kw.get('dilation', 1)!=1:
            raise NotImplementedError('dilation not implemented')
        if kw.get('output_padding', 0)!=0:
            print('CausalConvTranspose1d: output_padding is not tested')
        if kw.get('padding_mode', 'zeros')!='zeros':
            raise NotImplementedError('only zero-padding is implemented')
        if kernel_size//stride != kernel_size/stride:
            raise ValueError('kernel_size should be a multiple of stride')
        k = kernel_size
        s = stride
        super().__init__(in_channels, out_channels, k, s, padding=k-s, **kw)
        self.pad = torch.nn.ConstantPad1d(((k-s)//s, 0), 0.0)

    def forward(self, x):
        x = self.pad(x)
        # return super().forward(x)
        return nn.functional.conv_transpose1d(
            x,
            self.weight,
            self.bias,
            self.stride,
            self.padding,
            self.output_padding,
            self.groups,
            self.dilation,
        )


class CachedConvTranspose1d(nn.ConvTranspose1d):
    """
    Implementation of a ConvTranspose1d operation with cached padding
    """

    def __init__(self, *args, **kwargs):
        cd = kwargs.pop("cumulative_delay", 0)
        super().__init__(*args, **kwargs)
        stride = self.stride[0]
        self.initialized = 0
        self.cumulative_delay = self.padding[0] + cd * stride

    @torch.jit.unused
    @torch.no_grad()
    def init_cache(self, x):
        b, c, _ = x.shape
        self.register_buffer(
            "cache",
            torch.zeros(
                MAX_BATCH_SIZE,
                c,
                2 * self.padding[0],
            ).to(x))
        self.initialized += 1

    def forward(self, x):
        x = nn.functional.conv_transpose1d(
            x,
            self.weight,
            None,
            self.stride,
            0,
            self.output_padding,
            self.groups,
            self.dilation,
        )

        if not self.initialized:
            self.init_cache(x)

        padding = 2 * self.padding[0]

        x[..., :padding] += self.cache[:x.shape[0]]
        self.cache[:x.shape[0]].copy_(x[..., -padding:])

        x = x[..., :-padding]

        bias = self.bias
        if bias is not None:
            x = x + bias.unsqueeze(-1)
        return x


class ConvTranspose1d(nn.ConvTranspose1d):

    def __init__(self, *args, **kwargs) -> None:
        kwargs.pop("cumulative_delay", 0)
        super().__init__(*args, **kwargs)
        self.cumulative_delay = 0


class Conv1d(nn.Conv1d):

    def __init__(self, *args, **kwargs):
        self._pad = kwargs.get("padding", (0, 0))
        kwargs.pop("cumulative_delay", 0)
        kwargs["padding"] = 0

        super().__init__(*args, **kwargs)
        self.cumulative_delay = 0

    def forward(self, x):
        x = nn.functional.pad(x, self._pad)
        return nn.functional.conv1d(
            x,
            self.weight,
            self.bias,
            self.stride,
            self.padding,
            self.dilation,
            self.groups,
        )


class AlignBranches(nn.Module):

    def __init__(self, *branches, delays=None, cumulative_delay=0, stride=1):
        super().__init__()
        self.branches = nn.ModuleList(branches)

        if delays is None:
            delays = list(map(lambda x: x.cumulative_delay, self.branches))

        max_delay = max(delays)

        self.paddings = nn.ModuleList([
            CachedPadding1d(p, crop=True)
            for p in map(lambda f: max_delay - f, delays)
        ])

        self.cumulative_delay = int(cumulative_delay * stride) + max_delay

    def forward(self, x):
        outs = []
        for branch, pad in zip(self.branches, self.paddings):
            delayed_x = pad(x)
            outs.append(branch(delayed_x))
        return outs


class Branches(nn.Module):

    def __init__(self, *branches, delays=None, cumulative_delay=0, stride=1):
        super().__init__()
        self.branches = nn.ModuleList(branches)

        if delays is None:
            delays = list(map(lambda x: x.cumulative_delay, self.branches))

        max_delay = max(delays)

        self.cumulative_delay = int(cumulative_delay * stride) + max_delay

    def forward(self, x):
        outs = []
        for branch in self.branches:
            outs.append(branch(x))
        return outs
