import torch
import numpy as np

from torch import nn
from torch.nn import functional as F
from vector_quantize_pytorch.finite_scalar_quantization import FSQ


class RMSNorm(torch.nn.Module):
    def __init__(self, dim: int, eps: float = 1e-6):
        super().__init__()
        self.eps = eps
        self.weight = nn.Parameter(torch.ones(dim))

    def _norm(self, x):
        return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + self.eps)

    def forward(self, x):
        output = self._norm(x.float()).type_as(x)
        return output * self.weight


def modulate(x, shift, scale):
    return x * (1 + scale.unsqueeze(1)) + shift.unsqueeze(1)


def precompute_freqs_cis(dim: int, end: int, theta: float = 10000.0, theta_rescale_factor=1.):
    # proposed by reddit user bloc97, to rescale rotary embeddings to longer sequence length without fine-tuning
    # has some connection to NTK literature
    # https://www.reddit.com/r/LocalLLaMA/comments/14lz7j5/ntkaware_scaled_rope_allows_llama_models_to_have/
    # https://github.com/lucidrains/rotary-embedding-torch/blob/main/rotary_embedding_torch/rotary_embedding_torch.py
    theta *= theta_rescale_factor ** (dim / (dim - 2))
    freqs = 1.0 / (theta ** (torch.arange(0, dim, 2)[: (dim // 2)].float() / dim))
    t = torch.arange(end, device=freqs.device)  # type: ignore
    freqs = torch.outer(t, freqs).float()  # type: ignore
    freqs_cos = torch.cos(freqs)  # real part
    freqs_sin = torch.sin(freqs)  # imaginary part
    return torch.cat([freqs_cos, freqs_sin], dim=-1)


def reshape_for_broadcast(freqs_cis: torch.Tensor, x: torch.Tensor):
    ndim = x.ndim
    assert 0 <= 1 < ndim
    if ndim == 4:
        assert freqs_cis.shape == (x.shape[0], x.shape[1], x.shape[-1])
        shape = [d if i in (0, 1, ndim - 1) else 1 for i, d in enumerate(x.shape)]
        return freqs_cis.view(shape)
    elif ndim == 3:
        assert freqs_cis.shape == x.shape
        return freqs_cis
    else:
        raise ValueError(f"Unsupported ndim {ndim}")


def apply_rotary_emb(
    x: torch.Tensor,
    freqs_cis: torch.Tensor,
):
    if freqs_cis is None:
        return x

    x_r, x_i = x.float().reshape(x.shape[:-1] + (-1, 2)).unbind(-1)
    freqs_cos, freqs_sin = torch.chunk(freqs_cis, 2, dim=-1)
    # reshape freqs_cos and freqs_sin for broadcasting
    freqs_cos = reshape_for_broadcast(freqs_cos, x_r)
    freqs_sin = reshape_for_broadcast(freqs_sin, x_r)

    # apply rotation using real numbers
    x_out_r = x_r * freqs_cos - x_i * freqs_sin
    x_out_i = x_r * freqs_sin + x_i * freqs_cos

    # flatten last two dimensions
    x_out = torch.stack([x_out_r, x_out_i], dim=-1).flatten(x_out_r.ndim - 1)

    return x_out.type_as(x)


def sequence_mask(length, max_length=None):
    if max_length is None:
        max_length = length.max()
    x = torch.arange(max_length, dtype=length.dtype, device=length.device)
    return x.unsqueeze(0) < length.unsqueeze(1)


def score_mask(length, max_length=None):
    seq_mask = sequence_mask(length, max_length)
    sco_mask = torch.zeros_like(seq_mask, dtype=torch.float)
    sco_mask.masked_fill_(~seq_mask, float('-inf'))
    return sco_mask.unsqueeze(1).unsqueeze(2)


def score_mask_from_bool_mask(bool_mask):
    phone_mask = torch.zeros(*bool_mask.size(), device=bool_mask.device)
    phone_mask.masked_fill_(bool_mask, float('-inf'))
    phone_mask = phone_mask.unsqueeze(1).unsqueeze(2)
    return phone_mask


def _get_start(tensor, start):
    return (torch.zeros([tensor.size(0)], dtype=torch.long, device=tensor.device)
            if start is None else start)


def _get_len(tensor, length):
    return (torch.ones([tensor.size(0)], dtype=torch.long, device=tensor.device) * tensor.size(2)
            if length is None else length)


def get_pos_embed_indices(start, length, max_pos, scale=1.):
    # length = length if isinstance(length, int) else length.max()
    scale = scale * torch.ones_like(start, dtype=torch.float32)  # in case scale is a scalar
    pos = start.unsqueeze(1) + (
            torch.arange(length, device=start.device, dtype=torch.float32).unsqueeze(0) *
            scale.unsqueeze(1)).long()
    # avoid extra long error.
    pos = torch.where(pos < max_pos, pos, max_pos - 1)
    return pos


class FeedForward(nn.Module):
    def __init__(self, dim: int, hidden_dim: int, multiple_of: int = 256, drop: float = 0.):
        super().__init__()
        if hidden_dim is None:
            hidden_dim = 4 * dim
            hidden_dim = int(2 * hidden_dim / 3)
            hidden_dim = multiple_of * ((hidden_dim + multiple_of - 1) // multiple_of)
        self.w1 = nn.Linear(dim, hidden_dim, bias=False)
        self.w2 = nn.Linear(hidden_dim, dim, bias=False)
        self.w3 = nn.Linear(dim, hidden_dim, bias=False)
        self.dropout = nn.Dropout(drop)

    def forward(self, x):
        return self.dropout(self.w2(F.silu(self.w1(x)) * self.w3(x)))


class CrossAttention(nn.Module):
    def __init__(
        self,
        dim: int,
        num_heads: int = 8,
        qkv_bias: bool = False,
        qk_norm: bool = False,
        attn_drop: float = 0.,
        proj_drop: float = 0.,
        norm_layer: nn.Module = nn.LayerNorm,
    ) -> None:
        super().__init__()
        assert dim % num_heads == 0, 'dim should be divisible by num_heads'
        self.num_heads = num_heads
        self.head_dim = dim // num_heads
        self.scale = self.head_dim ** -0.5

        self.q = nn.Linear(dim, dim, bias=qkv_bias)
        self.kv = nn.Linear(dim, 2 * dim, bias=qkv_bias)
        self.q_norm = norm_layer(self.head_dim) if qk_norm else nn.Identity()
        self.k_norm = norm_layer(self.head_dim) if qk_norm else nn.Identity()
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

        # use flash attention or a manual implementation?
        self.flash = hasattr(torch.nn.functional, 'scaled_dot_product_attention')
        if not self.flash:
            print("WARNING: using slow attention. Flash Attention requires PyTorch >= 2.0")

    def forward(
        self,
        q_x: torch.Tensor,
        kv_x: torch.Tensor,
        q_freqs_cis: torch.Tensor,
        k_freqs_cis: torch.Tensor,
        mask: torch.Tensor) -> torch.Tensor:
        bsz, q_seqlen, ch = q_x.shape
        _, kv_seqlen, _ = kv_x.shape

        q = self.q(q_x).reshape(bsz, q_seqlen, self.num_heads, self.head_dim)
        kv = self.kv(kv_x).reshape(bsz, kv_seqlen, 2, self.num_heads, self.head_dim).permute(2, 0, 1, 3, 4)
        k, v = kv.unbind(0)
        q, k = self.q_norm(q), self.k_norm(k)
        q = apply_rotary_emb(q, q_freqs_cis)
        k = apply_rotary_emb(k, k_freqs_cis)

        # (bs, n_local_heads, q_seqlen, head_dim)
        q = q.transpose(1, 2)
        k = k.transpose(1, 2)
        v = v.transpose(1, 2)

        if self.flash:
            x = F.scaled_dot_product_attention(
                q, k, v, attn_mask=mask, dropout_p=self.attn_drop.p if self.training else 0.)
        else:
            q = q * self.scale
            attn = q @ k.transpose(-2, -1)
            if mask is not None:
                attn = attn + mask
            attn = attn.float().softmax(dim=-1).type_as(attn)
            attn = self.attn_drop(attn)
            x = attn @ v

        x = x.transpose(1, 2).contiguous().reshape(bsz, q_seqlen, ch)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x


class Attention(CrossAttention):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def forward(
        self,
        x: torch.Tensor,
        freqs_cis: torch.Tensor,
        mask: torch.Tensor
    ):
        return super().forward(x, x, freqs_cis, freqs_cis, mask)
