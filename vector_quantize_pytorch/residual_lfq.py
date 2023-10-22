from math import log2
from random import randrange
from functools import partial

import torch
from torch import nn
from torch.nn import Module, ModuleList
import torch.nn.functional as F
from torch.cuda.amp import autocast

from vector_quantize_pytorch.lookup_free_quantization import LFQ

from einops import rearrange, repeat, reduce, pack, unpack

# helper functions

def exists(val):
    return val is not None

def default(val, d):
    return val if exists(val) else d

def round_up_multiple(num, mult):
    return ceil(num / mult) * mult

# main class

class ResidualLFQ(Module):
    """ Follows Algorithm 1. in https://arxiv.org/pdf/2107.03312.pdf """

    def __init__(
        self,
        *,
        dim,
        num_quantizers,
        codebook_size,
        quantize_dropout = False,
        quantize_dropout_cutoff_index = 0,
        quantize_dropout_multiple_of = 1,
        **kwargs
    ):
        super().__init__()
        codebook_dim = int(log2(codebook_size))

        requires_projection = codebook_dim != dim
        self.project_in = nn.Linear(dim, codebook_dim) if requires_projection else nn.Identity()
        self.project_out = nn.Linear(codebook_dim, dim) if requires_projection else nn.Identity()

        self.num_quantizers = num_quantizers

        self.layers = nn.ModuleList([])

        for ind in range(num_quantizers):
            codebook_scale = 2 ** -ind

            lfq = LFQ(
                dim = codebook_dim,
                codebook_scale = codebook_scale,
                **kwargs
            )

            self.layers.append(lfq)

        self.quantize_dropout = quantize_dropout and num_quantizers > 1

        assert quantize_dropout_cutoff_index >= 0

        self.quantize_dropout_cutoff_index = quantize_dropout_cutoff_index
        self.quantize_dropout_multiple_of = quantize_dropout_multiple_of  # encodec paper proposes structured dropout, believe this was set to 4

    @property
    def codebooks(self):
        codebooks = [layer.codebook for layer in self.layers]
        codebooks = torch.stack(codebooks, dim = 0)
        return codebooks

    def get_codes_from_indices(self, indices):

        batch, quantize_dim = indices.shape[0], indices.shape[-1]

        # may also receive indices in the shape of 'b h w q' (accept_image_fmap)

        indices, ps = pack([indices], 'b * q')

        # because of quantize dropout, one can pass in indices that are coarse
        # and the network should be able to reconstruct

        if quantize_dim < self.num_quantizers:
            assert self.quantize_dropout > 0., 'quantize dropout must be greater than 0 if you wish to reconstruct from a signal with less fine quantizations'
            indices = F.pad(indices, (0, self.num_quantizers - quantize_dim), value = -1)

        # get ready for gathering

        codebooks = repeat(self.codebooks, 'q c d -> q b c d', b = batch)
        gather_indices = repeat(indices, 'b n q -> q b n d', d = codebooks.shape[-1])

        # take care of quantizer dropout

        mask = gather_indices == -1.
        gather_indices = gather_indices.masked_fill(mask, 0) # have it fetch a dummy code to be masked out later

        all_codes = codebooks.gather(2, gather_indices) # gather all codes

        # mask out any codes that were dropout-ed

        all_codes = all_codes.masked_fill(mask, 0.)

        # if (accept_image_fmap = True) then return shape (quantize, batch, height, width, dimension)

        all_codes, = unpack(all_codes, ps, 'q b * d')

        return all_codes

    def get_output_from_indices(self, indices):
        codes = self.get_codes_from_indices(indices)
        codes_summed = reduce(codes, 'q ... -> ...', 'sum')
        return self.project_out(codes_summed)

    def forward(
        self,
        x,
        return_all_codes = False
    ):
        num_quant, quant_dropout_multiple_of, device = self.num_quantizers, self.quantize_dropout_multiple_of, x.device

        x = self.project_in(x)

        quantized_out = 0.
        residual = x

        all_losses = []
        all_indices = []

        should_quantize_dropout = self.training and self.quantize_dropout

        # sample a layer index at which to dropout further residual quantization
        # also prepare null indices and loss

        if should_quantize_dropout:
            rand_quantize_dropout_index = randrange(self.quantize_dropout_cutoff_index, num_quant)

            if quant_dropout_multiple_of != 1:
                rand_quantize_dropout_index = round_up_multiple(rand_quantize_dropout_index + 1, quant_dropout_multiple_of) - 1

            null_indices = torch.full(x.shape[:2], -1., device = device, dtype = torch.long)
            null_loss = torch.tensor(0., device = device, dtype = x.dtype)

        # go through the layers

        with autocast(enabled = False):
            for quantizer_index, layer in enumerate(self.layers):

                if should_quantize_dropout and quantizer_index > rand_quantize_dropout_index:
                    all_indices.append(null_indices)
                    all_losses.append(null_loss)
                    continue

                quantized, indices, loss = layer(residual)

                residual = residual - quantized.detach()
                quantized_out = quantized_out + quantized

                all_indices.append(indices)
                all_losses.append(loss)

        # project out, if needed

        quantized_out = self.project_out(quantized_out)

        # stack all losses and indices

        all_losses, all_indices = map(partial(torch.stack, dim = -1), (all_losses, all_indices))

        ret = (quantized_out, all_indices, all_losses)

        if not return_all_codes:
            return ret

        # whether to return all codes from all codebooks across layers

        all_codes = self.get_codes_from_indices(all_indices)

        # will return all codes in shape (quantizer, batch, sequence length, codebook dimension)

        return (*ret, all_codes)