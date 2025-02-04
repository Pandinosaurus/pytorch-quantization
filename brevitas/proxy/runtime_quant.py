# Copyright (c) 2018-     Xilinx, Inc              (Alessandro Pappalardo)
# Copyright (c) 2016-     Facebook, Inc            (Adam Paszke)
# Copyright (c) 2014-     Facebook, Inc            (Soumith Chintala)
# Copyright (c) 2011-2014 Idiap Research Institute (Ronan Collobert)
# Copyright (c) 2012-2014 Deepmind Technologies    (Koray Kavukcuoglu)
# Copyright (c) 2011-2012 NEC Laboratories America (Koray Kavukcuoglu)
# Copyright (c) 2011-2013 NYU                      (Clement Farabet)
# Copyright (c) 2006-2010 NEC Laboratories America (Ronan Collobert, Leon Bottou, Iain Melvin, Jason Weston)
# Copyright (c) 2006      Idiap Research Institute (Samy Bengio)
# Copyright (c) 2001-2004 Idiap Research Institute (Ronan Collobert, Samy Bengio, Johnny Mariethoz)

# All rights reserved.

# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:

# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.

# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.

# 3. Neither the names of Xilinx, Facebook, Deepmind Technologies, NYU,
#    NEC Laboratories America and IDIAP Research Institute nor the names
#    of its contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.

# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

from typing import Optional, Tuple

import torch
from torch.nn import Module

from brevitas.core import ZERO_HW_SENTINEL_NAME, ZERO_HW_SENTINEL_VALUE
from brevitas.core.bit_width import BitWidthImplType, MsbClampParameterBitWidth, BitWidthConst
from brevitas.core.bit_width import BitWidthParameter, LsbTruncParameterBitWidth, ZeroLsbTruncBitWidth
from brevitas.core.function_wrapper import TensorClamp
from brevitas.core.quant import PrescaledRestrictIntQuantWithInputBitWidth, ClampedBinaryQuant
from brevitas.core.quant import QuantType, IdentityPrescaledIntQuant, PrescaledIntQuant
from brevitas.core.quant import RescalingIntQuant, IdentityQuant
from brevitas.core.restrict_val import RestrictValueType, RestrictValue, FloatToIntImplType
from brevitas.core.scaling import RuntimeStatsScaling, SCALING_SCALAR_SHAPE, StatsInputViewShapeImpl
from brevitas.core.scaling import ScalingImplType, StandaloneScaling, IntScaling
from brevitas.core.stats import StatsOp


class FusedActivationQuantProxy(torch.jit.ScriptModule):

    def __init__(self,
                 activation_impl,
                 tensor_quant):
        super(FusedActivationQuantProxy, self).__init__()
        self.activation_impl = activation_impl
        self.tensor_quant = tensor_quant

    @torch.jit.script_method
    def forward(self, x, zero_hw_sentinel):
        x = self.activation_impl(x)
        x, output_scale, output_bit_width = self.tensor_quant(x, zero_hw_sentinel)
        return x, output_scale, output_bit_width


class ActivationQuantProxy(Module):

    def __init__(self,
                 activation_impl: Module,
                 bit_width: int,
                 signed: bool,
                 narrow_range: bool,
                 min_val: float,
                 max_val: float,
                 quant_type: QuantType,
                 float_to_int_impl_type: FloatToIntImplType,
                 scaling_override: Optional[Module],
                 scaling_impl_type: ScalingImplType,
                 scaling_per_channel: bool,
                 scaling_min_val: Optional[float],
                 scaling_stats_sigma: Optional[float],
                 scaling_stats_op: Optional[StatsOp],
                 scaling_stats_buffer_momentum: Optional[float],
                 scaling_stats_input_view_shape_impl: Optional[StatsInputViewShapeImpl],
                 scaling_stats_permute_dims: Optional[Tuple],
                 per_channel_broadcastable_shape: Optional[Tuple[int, ...]],
                 min_overall_bit_width: Optional[int],
                 max_overall_bit_width: Optional[int],
                 bit_width_impl_override: Module,
                 bit_width_impl_type: BitWidthImplType,
                 restrict_bit_width_type: RestrictValueType,
                 restrict_scaling_type: RestrictValueType,
                 override_pretrained_bit_width: bool):
        super(ActivationQuantProxy, self).__init__()

        if not signed and min_val != 0.0:
            raise Exception("Min val has to be 0.0 when quantization is unsigned.")
        if scaling_per_channel and per_channel_broadcastable_shape is None:
            raise Exception("Per channel scaling requires to specify number of channels.")

        self.register_buffer(ZERO_HW_SENTINEL_NAME, torch.tensor(ZERO_HW_SENTINEL_VALUE))

        if scaling_per_channel and not scaling_stats_op == StatsOp.MAX_AVE:
            scaling_shape = per_channel_broadcastable_shape
            scaling_stats_reduce_dim = 1
        elif not scaling_per_channel and scaling_stats_op == StatsOp.MAX_AVE:
            raise Exception("Not supported yet")
        else:  # not scaling_per_channel
            scaling_shape = SCALING_SCALAR_SHAPE
            scaling_stats_input_view_shape_impl = StatsInputViewShapeImpl.OVER_TENSOR
            scaling_stats_reduce_dim = None
            scaling_stats_permute_dims = None

        if quant_type == QuantType.FP:
            tensor_quant = IdentityQuant()

        else:
            if scaling_impl_type != ScalingImplType.OVERRIDE and scaling_override is not None:
                raise Exception("Overriding scaling requires to set ScalingImplType to OVERRIDE explicitly.")
            if scaling_impl_type == ScalingImplType.OVERRIDE and scaling_override is None:
                raise Exception("Overriding scaling requires to pass a scaling impl module.")

            if scaling_impl_type == ScalingImplType.OVERRIDE and scaling_override is not None:
                scaling_impl = scaling_override
                runtime = False

            elif scaling_impl_type == ScalingImplType.CONST or scaling_impl_type == ScalingImplType.PARAMETER:
                scaling_init = RescalingIntQuant.scaling_init_from_min_max(min_val, max_val)
                scaling_impl = StandaloneScaling(is_parameter=scaling_impl_type == ScalingImplType.PARAMETER,
                                                 parameter_shape=scaling_shape,
                                                 restrict_scaling_type=restrict_scaling_type,
                                                 scaling_init=scaling_init,
                                                 scaling_min_val=scaling_min_val)
                runtime = False
            elif scaling_impl_type == ScalingImplType.STATS or scaling_impl_type == ScalingImplType.AFFINE_STATS:
                stats_buffer_init = RescalingIntQuant.scaling_init_from_min_max(min_val, max_val).item()
                scaling_impl = RuntimeStatsScaling(stats_op=scaling_stats_op,
                                                   restrict_scaling_type=restrict_scaling_type,
                                                   stats_input_view_shape_impl=scaling_stats_input_view_shape_impl,
                                                   stats_output_shape=scaling_shape,
                                                   sigma=scaling_stats_sigma,
                                                   scaling_min_val=scaling_min_val,
                                                   stats_reduce_dim=scaling_stats_reduce_dim,
                                                   stats_buffer_momentum=scaling_stats_buffer_momentum,
                                                   stats_buffer_init=stats_buffer_init,
                                                   stats_permute_dims=scaling_stats_permute_dims,
                                                   affine=scaling_impl_type == ScalingImplType.AFFINE_STATS)
                runtime = True
            else:
                raise Exception("Scaling type {} not supported for int runtime quantization"
                                .format(str(scaling_impl_type)))

            if quant_type == QuantType.BINARY:
                if not signed:
                    raise Exception("Binary activation supports only signed activations")
                tensor_quant = ClampedBinaryQuant(scaling_impl=scaling_impl)

            elif quant_type == QuantType.INT:

                if bit_width_impl_override is None:
                    if bit_width_impl_type is None or bit_width is None or restrict_bit_width_type is None:
                        raise Exception("Bit width is not defined properly")

                    if bit_width_impl_type == BitWidthImplType.CONST:
                        tensor_clamp_impl = TensorClamp()  # If it's const, don't pass gradients to clipped values
                        msb_clamp_bit_width_impl = BitWidthConst(bit_width, restrict_bit_width_type)
                    elif bit_width_impl_type == BitWidthImplType.PARAMETER:
                        tensor_clamp_impl = TensorClamp()  # if it's learned, I pass gradients to the bit width
                        msb_clamp_bit_width_impl = BitWidthParameter(bit_width,
                                                                     min_overall_bit_width,
                                                                     max_overall_bit_width,
                                                                     restrict_bit_width_type,
                                                                     override_pretrained_bit_width)
                    else:
                        raise Exception("Bit width type {} not supported for weight quantization"
                                        .format(str(bit_width_impl_type)))
                else:
                    msb_clamp_bit_width_impl = bit_width_impl_override
                    tensor_clamp_impl = TensorClamp()  # if there is an override, it's learned

                float_to_int_impl = RestrictValue(restrict_value_type=RestrictValueType.INT,
                                                  float_to_int_impl_type=float_to_int_impl_type,
                                                  min_val=None)
                int_scaling_impl = IntScaling(narrow_range,
                                              signed=signed,
                                              restrict_scaling_type=restrict_scaling_type)
                tensor_quant = RescalingIntQuant(signed=signed,
                                                 narrow_range=narrow_range,
                                                 scaling_impl=scaling_impl,
                                                 int_scaling_impl=int_scaling_impl,
                                                 tensor_clamp_impl=tensor_clamp_impl,
                                                 msb_clamp_bit_width_impl=msb_clamp_bit_width_impl,
                                                 float_to_int_impl=float_to_int_impl,
                                                 runtime=runtime)
            else:
                raise Exception("Quantization type {} not supported for activations.".format(quant_type))

        self.fused_activation_quant_proxy = FusedActivationQuantProxy(activation_impl, tensor_quant)

    def forward(self, x):
        output, output_scale, output_bit_width = self.fused_activation_quant_proxy(x, self.zero_hw_sentinel)
        return output, output_scale, output_bit_width

    def _load_from_state_dict(self, state_dict, prefix, local_metadata, strict,
                              missing_keys, unexpected_keys, error_msgs):
        super(ActivationQuantProxy, self)._load_from_state_dict(state_dict, prefix, local_metadata, strict,
            missing_keys, unexpected_keys, error_msgs)
        zero_hw_sentinel_key = prefix + ZERO_HW_SENTINEL_NAME
        if zero_hw_sentinel_key in missing_keys:
            missing_keys.remove(zero_hw_sentinel_key)


class ClampQuantProxy(Module):

    def __init__(self,
                 signed: bool,
                 narrow_range: bool,
                 quant_type: QuantType,
                 ms_bit_width_to_clamp: int,
                 clamp_at_least_init_val: bool,
                 min_overall_bit_width: Optional[int],
                 max_overall_bit_width: Optional[int],
                 msb_clamp_bit_width_impl_type: BitWidthImplType,
                 override_pretrained_bit_width: bool):
        super(ClampQuantProxy, self).__init__()
        self.register_buffer(ZERO_HW_SENTINEL_NAME, torch.tensor(ZERO_HW_SENTINEL_VALUE))

        if quant_type == QuantType.FP:
            self.tensor_quant = IdentityPrescaledIntQuant()

        elif quant_type == QuantType.INT:
            msb_clamp_bit_width_impl = MsbClampParameterBitWidth(ms_bit_width_to_clamp=ms_bit_width_to_clamp,
                                                                 clamp_at_least_init_val=clamp_at_least_init_val,
                                                                 min_overall_bit_width=min_overall_bit_width,
                                                                 max_overall_bit_width=max_overall_bit_width,
                                                                 bit_width_impl_type=msb_clamp_bit_width_impl_type,
                                                                 override_pretrained=override_pretrained_bit_width)
            tensor_clamp_impl = TensorClamp()
            float_to_int_impl = RestrictValue(restrict_value_type=RestrictValueType.INT,
                                              float_to_int_impl_type=FloatToIntImplType.ROUND,
                                              min_val=None)
            tensor_quant_impl = PrescaledRestrictIntQuantWithInputBitWidth
            self.tensor_quant = tensor_quant_impl(signed=signed,
                                                  narrow_range=narrow_range,
                                                  tensor_clamp_impl=tensor_clamp_impl,
                                                  float_to_int_impl=float_to_int_impl,
                                                  msb_clamp_bit_width_impl=msb_clamp_bit_width_impl)
        else:
            raise Exception("Quantization type {} not supported for accumulators.".format(quant_type))

    def forward(self, x, input_scale, input_bit_width):
        x, output_scale, output_bit_width = self.tensor_quant(x, input_scale, input_bit_width, self.zero_hw_sentinel)
        return x, output_scale, output_bit_width

    def _load_from_state_dict(self, state_dict, prefix, local_metadata, strict,
                              missing_keys, unexpected_keys, error_msgs):
        super(ClampQuantProxy, self)._load_from_state_dict(state_dict, prefix, local_metadata, strict,
                                                            missing_keys, unexpected_keys, error_msgs)
        zero_hw_sentinel_key = prefix + ZERO_HW_SENTINEL_NAME
        if zero_hw_sentinel_key in missing_keys:
            missing_keys.remove(zero_hw_sentinel_key)


class TruncQuantProxy(Module):

    def __init__(self,
                 signed: bool,
                 quant_type: QuantType,
                 ls_bit_width_to_trunc: int,
                 trunc_at_least_init_val: bool,
                 min_overall_bit_width: Optional[int],
                 max_overall_bit_width: Optional[int],
                 lsb_trunc_bit_width_impl_type: BitWidthImplType,
                 explicit_rescaling: bool,
                 override_pretrained_bit_width: bool):
        super(TruncQuantProxy, self).__init__()
        self.register_buffer(ZERO_HW_SENTINEL_NAME, torch.tensor(ZERO_HW_SENTINEL_VALUE))
        self.explicit_rescaling = explicit_rescaling

        if quant_type == QuantType.FP:
            self.lsb_trunc_bit_width_impl = ZeroLsbTruncBitWidth()
            self.tensor_quant = IdentityPrescaledIntQuant()

        elif quant_type == QuantType.INT:
            self.lsb_trunc_bit_width_impl = LsbTruncParameterBitWidth(ls_bit_width_to_trunc=ls_bit_width_to_trunc,
                                                                      trunc_at_least_init_val=trunc_at_least_init_val,
                                                                      min_overall_bit_width=min_overall_bit_width,
                                                                      max_overall_bit_width=max_overall_bit_width,
                                                                      bit_width_impl_type=lsb_trunc_bit_width_impl_type,
                                                                      override_pretrained=override_pretrained_bit_width)
            tensor_clamp_impl = TensorClamp()
            float_to_int_impl = RestrictValue(restrict_value_type=RestrictValueType.INT,
                                              float_to_int_impl_type=FloatToIntImplType.FLOOR,
                                              min_val=None)
            self.tensor_quant = PrescaledIntQuant(signed=signed,
                                                  narrow_range=False,
                                                  tensor_clamp_impl=tensor_clamp_impl,
                                                  float_to_int_impl=float_to_int_impl)
        else:
            raise Exception("Quantization type {} not supported for accumulators.".format(quant_type))

    def forward(self, x, input_scale, input_bit_width):
        trunc_bit_width = self.lsb_trunc_bit_width_impl(input_bit_width, self.zero_hw_sentinel)
        trunc_scale = 2.0 ** trunc_bit_width
        output_scale = trunc_scale * input_scale
        x, output_scale, input_bit_width = self.tensor_quant(x, output_scale, input_bit_width, self.zero_hw_sentinel)
        if self.explicit_rescaling:
            x = x / trunc_scale # rescaling is explicit, so the truncation scale stays with x rather with output_scale
            output_scale = output_scale / trunc_scale
        output_bit_width = input_bit_width - trunc_bit_width
        return x, output_scale, output_bit_width

    def _load_from_state_dict(self, state_dict, prefix, local_metadata, strict,
                              missing_keys, unexpected_keys, error_msgs):
        super(TruncQuantProxy, self)._load_from_state_dict(state_dict, prefix, local_metadata, strict,
                                                            missing_keys, unexpected_keys, error_msgs)
        zero_hw_sentinel_key = prefix + ZERO_HW_SENTINEL_NAME
        if zero_hw_sentinel_key in missing_keys:
            missing_keys.remove(zero_hw_sentinel_key)
