import torch

import comfy.utils
import comfy.controlnet as comfy_cn
from comfy.controlnet import  ControlNet, T2IAdapter, broadcast_image_to

ControlNetWeightsType = list[float]
T2IAdapterWeightsType = list[float]


class LatentKeyframe:
    def __init__(self, batch_index: int, strength: float) -> None:
        self.batch_index = batch_index
        self.strength = strength


# always maintain sorted state (by batch_index of LatentKeyframe)
class LatentKeyframeGroup:
    def __init__(self) -> None:
        self.keyframes: list[LatentKeyframe] = []

    def add(self, keyframe: LatentKeyframe) -> None:
        added = False
        # replace existing keyframe if same batch_index
        for i in range(len(self.keyframes)):
            if self.keyframes[i].batch_index == keyframe.batch_index:
                self.keyframes[i] = keyframe
                added = True
                break
        if not added:
            self.keyframes.append(keyframe)
        self.keyframes.sort(key=lambda k: k.batch_index)
    
    def get_index(self, index: int) -> LatentKeyframe | None:
        try:
            return self.keyframes[index]
        except IndexError:
            return None
    
    def __getitem__(self, index) -> LatentKeyframe:
        return self.keyframes[index]
    
    def is_empty(self) -> bool:
        return len(self.keyframes) == 0


class TimestepKeyframe:
    def __init__(self,
                 start_percent: float = 0.0,
                 control_net_weights: ControlNetWeightsType = None,
                 t2i_adapter_weights: T2IAdapterWeightsType = None,
                 latent_keyframes: LatentKeyframeGroup = None) -> None:
        self.start_percent = start_percent
        self.control_net_weights = control_net_weights
        self.t2i_adapter_weights = t2i_adapter_weights
        self.latent_keyframes = latent_keyframes
    
    
    @classmethod
    def default(cls) -> 'TimestepKeyframe':
        return cls(0.0)


# always maintain sorted state (by start_percent of TimestepKeyFrame)
class TimestepKeyframeGroup:
    def __init__(self) -> None:
        self.keyframes: list[TimestepKeyframe] = []
        self.keyframes.append(TimestepKeyframe.default())

    def add(self, keyframe: TimestepKeyframe) -> None:
        added = False
        # replace existing keyframe if same start_percent
        for i in range(len(self.keyframes)):
            if self.keyframes[i].start_percent == keyframe.start_percent:
                self.keyframes[i] = keyframe
                added = True
                break
        if not added:
            self.keyframes.append(keyframe)
        self.keyframes.sort(key=lambda k: k.start_percent)

    def get_index(self, index: int) -> TimestepKeyframe | None:
        try:
            return self.keyframes[index]
        except IndexError:
            return None
    
    def __getitem__(self, index) -> TimestepKeyframe:
        return self.keyframes[index]
    
    def is_empty(self) -> bool:
        return len(self.keyframes) == 0
    
    @classmethod
    def default(cls, keyframe: TimestepKeyframe) -> 'TimestepKeyframeGroup':
        group = cls()
        group.keyframes[0] = keyframe
        return group


# used to inject ControlNetAdvanced and T2IAdapterAdvanced control_merge function
def control_merge_inject(self, control_input, control_output, control_prev, output_dtype):
    out = {'input':[], 'middle':[], 'output': []}

    if control_input is not None:
        for i in range(len(control_input)):
            key = 'input'
            x = control_input[i]
            if x is not None:
                self.apply_advanced_strengths_and_masks(x, self.current_timestep_keyframe, self.batched_number)

                x *= self.strength * self.weights[i]
                if x.dtype != output_dtype:
                    x = x.to(output_dtype)
            out[key].insert(0, x)

    if control_output is not None:
        for i in range(len(control_output)):
            if i == (len(control_output) - 1):
                key = 'middle'
                index = 0
            else:
                key = 'output'
                index = i
            x = control_output[i]
            if x is not None:
                self.apply_advanced_strengths_and_masks(x, self.current_timestep_keyframe, self.batched_number)

                if self.global_average_pooling:
                    x = torch.mean(x, dim=(2, 3), keepdim=True).repeat(1, 1, x.shape[2], x.shape[3])

                x *= self.strength * self.weights[i]
                if x.dtype != output_dtype:
                    x = x.to(output_dtype)

            out[key].append(x)
    if control_prev is not None:
        for x in ['input', 'middle', 'output']:
            o = out[x]
            for i in range(len(control_prev[x])):
                prev_val = control_prev[x][i]
                if i >= len(o):
                    o.append(prev_val)
                elif prev_val is not None:
                    if o[i] is None:
                        o[i] = prev_val
                    else:
                        o[i] += prev_val
    return out


class ControlNetAdvanced(ControlNet):
    def __init__(self, control_model, timestep_keyframes: TimestepKeyframeGroup, global_average_pooling=False, device=None):
        super().__init__(control_model=control_model, global_average_pooling=global_average_pooling, device=device)
        self.timestep_keyframes = timestep_keyframes if timestep_keyframes else TimestepKeyframeGroup()
        self.current_timestep_keyframe = self.timestep_keyframes.keyframes[0]
        # initialize weights
        self.weights = self.timestep_keyframes.keyframes[0].control_net_weights if self.timestep_keyframes.keyframes[0].control_net_weights else [1.0]*13
        # mask for which parts of controlnet output to keep
        self.cond_hint_mask = None
        # actual index values
        self.sub_idxs = None
        self.full_latent_length = 0
        self.context_length = 0
        # override control_merge
        self.control_merge = control_merge_inject.__get__(self, type(self))

    def set_cond_hint_mask(self, mask_hint):
        self.cond_hint_mask = mask_hint
        return self

    def get_control(self, x_noisy, t, cond, batched_number):
        # need to reference t and batched_number later
        self.t = t
        self.batched_number = batched_number
        # TODO: choose TimestepKeyframe based on t
        if self.sub_idxs is not None:
            # perform special version of get_control
            return self.sliding_get_control(x_noisy, t, cond, batched_number)
        else:
            return super().get_control(x_noisy, t, cond, batched_number)

    def sliding_get_control(self, x_noisy, t, cond, batched_number):
        control_prev = None
        if self.previous_controlnet is not None:
            control_prev = self.previous_controlnet.get_control(x_noisy, t, cond, batched_number)

        if self.timestep_range is not None:
            if t[0] > self.timestep_range[0] or t[0] < self.timestep_range[1]:
                if control_prev is not None:
                    return control_prev
                else:
                    return None

        output_dtype = x_noisy.dtype

        # TODO: change this to not require cond_hint upscaling every step
        if self.sub_idxs is not None or self.self.cond_hint is None or x_noisy.shape[2] * 8 != self.cond_hint.shape[2] or x_noisy.shape[3] * 8 != self.cond_hint.shape[3]:
            if self.cond_hint is not None:
                del self.cond_hint
            self.cond_hint = None
            self.cond_hint = comfy.utils.common_upscale(self.cond_hint_original, x_noisy.shape[3] * 8, x_noisy.shape[2] * 8, 'nearest-exact', "center").to(self.control_model.dtype).to(self.device)
            # if self.cond_hint length matches real latent count, need to subdivide it
            if self.cond_hint.size(0) == self.full_latent_length:
                self.cond_hint = self.cond_hint[self.sub_idxs]
        
        if x_noisy.shape[0] != self.cond_hint.shape[0]:
            self.cond_hint = broadcast_image_to(self.cond_hint, x_noisy.shape[0], batched_number)

        context = cond['c_crossattn']
        y = cond.get('c_adm', None)
        if y is not None:
            y = y.to(self.control_model.dtype)
        control = self.control_model(x=x_noisy.to(self.control_model.dtype), hint=self.cond_hint, timesteps=t, context=context.to(self.control_model.dtype), y=y)
        return self.control_merge(None, control, control_prev, output_dtype)

    def apply_advanced_strengths_and_masks(self, x, current_timestep_keyframe: TimestepKeyframe, batched_number: int):
        if current_timestep_keyframe.latent_keyframes is not None:
            # apply strengths, and get batch indeces to zero out
            # AKA latents that should not be influenced by ControlNet
            latent_count = x.size(0)//batched_number

            indeces_to_zero = set(range(latent_count))
            mapped_indeces = None
            # if expecting subdivision, will need to translate between subset and actual idx values
            if self.sub_idxs:
                mapped_indeces = {}
                for i, actual in enumerate(self.sub_idxs):
                    mapped_indeces[actual] = i
            for keyframe in current_timestep_keyframe.latent_keyframes:
                real_index = keyframe.batch_index
                # if not mapping indeces, what you see is what you get
                if mapped_indeces is None:
                    if real_index in indeces_to_zero:
                        indeces_to_zero.remove(keyframe.batch_index)
                # otherwise, see if batch_index is even included in this set of latents
                else:
                    real_index = mapped_indeces.get(keyframe.batch_index, None)
                    if real_index is None:
                        continue
                    indeces_to_zero.remove(real_index)

                # apply strength for each batched cond/uncond
                for b in range(batched_number):
                    x[(latent_count*b)+real_index] *= keyframe.strength

            # zero them out by multiplying by zero
            for batch_index in indeces_to_zero:
                # apply zero for each batched cond/uncond
                for b in range(batched_number):
                    x[(latent_count*b)+batch_index] *= 0.0

    def copy(self):
        c = ControlNetAdvanced(self.control_model, self.timestep_keyframes, global_average_pooling=self.global_average_pooling)
        self.copy_to(c)
        return c

    def cleanup(self):
        super().cleanup()
        self.sub_idxs = None
        self.full_latent_length = 0
        self.context_length = 0


class T2IAdapterAdvanced(T2IAdapter):
    def __init__(self, t2i_model, timestep_keyframes: TimestepKeyframeGroup, channels_in, device=None):
        super().__init__(t2i_model=t2i_model, channels_in=channels_in, device=device)
        self.timestep_keyframes = timestep_keyframes if timestep_keyframes else TimestepKeyframeGroup()
        self.current_timestep_keyframe = self.timestep_keyframes.keyframes[0]
        first_weight = self.timestep_keyframes.keyframes[0].t2i_adapter_weights if self.timestep_keyframes.get_index(0) else None
        self.weights = first_weight if first_weight else [1.0]*12
        # mask for which parts of controlnet output to keep
        self.cond_hint_mask = None
        # actual index values
        self.sub_idxs = None
        self.full_latent_length = 0
        self.context_length = 0
        # override control_merge
        self.control_merge = control_merge_inject.__get__(self, type(self))
    
    def get_control(self, x_noisy, t, cond, batched_number):
        # need to reference t and batched_number later
        self.t = t
        self.batched_number = batched_number
        # TODO: choose TimestepKeyframe based on t
        try:
            # if sub indexes present, replace original hint with subsection
            if self.sub_idxs is not None:
                full_cond_hint_original = self.cond_hint_original
                del self.cond_hint
                self.cond_hint = None
                self.cond_hint_original = full_cond_hint_original[self.sub_idxs]
            return super().get_control(x_noisy, t, cond, batched_number)
        finally:
            if self.sub_idxs is not None:
                # replace original cond hint
                self.cond_hint_original = full_cond_hint_original
                del full_cond_hint_original

    def apply_advanced_strengths_and_masks(self, x, current_timestep_keyframe: TimestepKeyframe, batched_number: int):
        # For now, do nothing; need to figure out LatentKeyframe control is even possible for T2I Adapters
        return

    def copy(self):
        c = T2IAdapterAdvanced(self.t2i_model, self.timestep_keyframes, self.channels_in)
        self.copy_to(c)
        return c
    
    def cleanup(self):
        super().cleanup()
        self.sub_idxs = None
        self.full_latent_length = 0
        self.context_length = 0


def load_controlnet(ckpt_path, timestep_keyframe: TimestepKeyframeGroup=None, model=None):
    def load_t2i_adapter(t2i_data):
        adapter = comfy_cn.load_t2i_adapter(t2i_data)
        return T2IAdapterAdvanced(adapter.t2i_model, timestep_keyframe, adapter.channels_in)

    # override load_t2i_adapter
    original_load_t2i_adapter = comfy_cn.load_t2i_adapter
    comfy_cn.load_t2i_adapter = load_t2i_adapter

    try:
        control = comfy_cn.load_controlnet(ckpt_path, model=model)
        if isinstance(control, T2IAdapterAdvanced):
            return control

        return ControlNetAdvanced(control.control_model, timestep_keyframe, global_average_pooling=control.global_average_pooling)
    except:
        raise
    finally:
        # restore original load_t2i_adapter
        comfy_cn.load_t2i_adapter = original_load_t2i_adapter
