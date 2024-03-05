import torch
import torch.nn as nn
import torch.nn.functional as F

from .attention_kernel_integral import AttentionKernelIntegral
from .mlp import MLPLinear
from .embeddings import SirenNet, GaussianFourierFeatureTransform
from .skip_connections import skip_connection


def get_normalization(norm, channels):
    if norm == 'none':
        norm_fn = nn.Identity()
    elif norm == "instance_norm":
        norm_fn = nn.InstanceNorm1d(channels)
    elif norm == "group_norm":
        norm_fn = nn.GroupNorm(num_groups=32 if channels > 128 else 1, num_channels=channels)
    elif norm == 'layer_norm':
        norm_fn = nn.LayerNorm(channels)
    else:
        raise ValueError(
            f"Got norm={norm} but expected None or one of "
            "[instance_norm, group_norm, layer_norm]"
        )
    return norm_fn


def normalize(u, norm_fn):
    # transform into channel first, from: B N C to: B C N
    if isinstance(norm_fn, nn.GroupNorm) or isinstance(norm_fn, nn.InstanceNorm1d):
        u = u.permute(0, 2, 1).contiguous()
        u = norm_fn(u)
        u = u.permute(0, 2, 1).contiguous()
    else:
        u = norm_fn(u)
    return u


class TransformerEncoderBlock(nn.Module):
    """
    Transformer Encoder Block with self-attention and FFN,
    use pre-normalization.
    """
    def __init__(
            self,
            in_channels,
            out_channels,
            hidden_channels,
            num_heads,
            head_n_channels,
            use_mlp=True,
            mlp_dropout=0,
            mlp_expansion=2.0,
            non_linearity=F.gelu,
            norm='layer_norm',
            **kwargs,
    ):
        super().__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.hidden_channels = hidden_channels
        self.num_heads = num_heads
        self.head_n_channels = head_n_channels
        self.use_mlp = use_mlp
        self.mlp_dropout = mlp_dropout
        self.mlp_expansion = mlp_expansion
        self.non_linearity = non_linearity
        self.norm = norm

        self.lifting = nn.Linear(self.in_channels, self.hidden_channels) \
            if self.in_channels != self.hidden_channels else nn.Identity()

        self.to_out = nn.Linear(self.hidden_channels, self.out_channels) \
            if self.hidden_channels != self.out_channels else nn.Identity()

        self.attention_norm = get_normalization(self.norm, self.hidden_channels)
        self.attention_layer = AttentionKernelIntegral(
                                    in_channels=self.hidden_channels,
                                    out_channels=self.hidden_channels,
                                    n_heads=self.num_heads,
                                    head_n_channels=self.head_n_channels,
                                    project_query=True)

        if self.use_mlp:
            self.mlp_norm = get_normalization(self.norm, self.hidden_channels)
            self.mlp_layer = MLPLinear([self.hidden_channels,
                                       int(self.hidden_channels * self.mlp_expansion),
                                       self.hidden_channels],
                                      dropout=self.mlp_dropout)

    def forward(self,
                u,
                pos,
                pos_emb_module=None,
                **kwargs):
        u = self.lifting(u)
        u_attention_skip = u
        u = self.attention_layer(u_src=normalize(u, self.attention_norm),
                                 pos_src=pos,
                                 positional_embedding_module=pos_emb_module,
                                 **kwargs)
        u = u + u_attention_skip
        if self.use_mlp:
            u_mlp_skip = u
            u = self.mlp_layer(normalize(u, self.mlp_norm))
            u = u + u_mlp_skip
        u = self.to_out(u)
        return u


# Note: this is not a causal-attention-based Transformer decoder as in language models
# but rather a "decoder" that maps from the latent grid to the output grid.
class TransformerDecoderBlock(nn.Module):
    def __init__(
            self,
            n_dim,
            in_channels,
            out_channels,
            hidden_channels,
            num_heads,
            head_n_channels,
            query_basis='siren',
            use_mlp=True,
            mlp_dropout=0,
            mlp_expansion=2.0,
            non_linearity=F.gelu,
            query_siren_layers=3,
            query_fourier_scale=2.0,
            norm='layer_norm',
            **kwargs,
    ):
        super().__init__()
        self.n_dim = n_dim
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.hidden_channels = hidden_channels
        self.num_heads = num_heads
        self.head_n_channels = head_n_channels
        self.use_mlp = use_mlp
        self.mlp_dropout = mlp_dropout
        self.mlp_expansion = mlp_expansion
        self.non_linearity = non_linearity
        self.norm = norm

        self.query_basis = query_basis
        self.query_siren_layers = query_siren_layers
        self.query_fourier_scale = query_fourier_scale

        self.lifting = nn.Linear(self.in_channels, self.hidden_channels) \
            if self.in_channels != self.hidden_channels else nn.Identity()

        self.out_norm = get_normalization(self.norm, self.hidden_channels)
        self.to_out = MLPLinear([self.hidden_channels, self.hidden_channels, self.out_channels],
                                non_linearity=self.non_linearity)

        # build basis for decoder
        if self.query_basis == 'siren':
            self.query_basis_fn = SirenNet(dim_in=self.n_dim,
                                           dim_hidden=self.hidden_channels,
                                           dim_out=self.num_heads * self.head_n_channels,
                                           num_layers=self.query_siren_layers)
        elif self.query_basis == 'fourier':
            self.query_basis_fn = nn.Sequential(
                GaussianFourierFeatureTransform(self.n_dim,
                                                mapping_size=self.head_n_channels,
                                                scale=self.query_fourier_scale),
                nn.Linear(self.head_n_channels * 2, self.num_heads * self.head_n_channels))
        elif self.query_basis == 'linear':
            self.query_basis_fn = nn.Linear(self.n_dim, num_heads * self.head_n_channels)
        else:
            raise ValueError(f'query_basis must be one of ["siren", "fourier", "linear"], got {self.query_basis}')

        self.attention_norm = get_normalization(self.norm, self.hidden_channels)
        self.attention_layer = AttentionKernelIntegral(in_channels=self.hidden_channels,
                                                        out_channels=self.hidden_channels,
                                                        n_heads=self.num_heads,
                                                        head_n_channels=self.head_n_channels,
                                                        project_query=False)

    def forward(self,
                u,
                pos_src,
                pos_emb_module=None,
                pos_qry=None,
                **kwargs
                ):
        u = self.lifting(u)
        if pos_qry is None:
            pos_qry = pos_src  # assume that the query points are the same as the source points
        query_emb = self.query_basis_fn(pos_qry)
        query_emb = query_emb.view(pos_qry.shape[0], -1, self.num_heads * self.head_n_channels)
        if query_emb.shape[0] != u.shape[0]:
            query_emb = query_emb.expand(u.shape[0], -1, -1)
        u_out = self.attention_layer(u_src=u,
                                     pos_src=pos_src,
                                     u_qry=query_emb,
                                     pos_qry=pos_qry,
                                     positional_embedding_module=pos_emb_module,
                                     **kwargs)
        u_out = self.to_out(normalize(u_out, self.out_norm))
        return u_out








