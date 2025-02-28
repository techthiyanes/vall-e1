"""
# an AR + NAR model that handles:
* inferencing the primary RVQ level in an autoregressive manner (AR)
* inferencing the remaining RVQ levels in parallel (NAR)

This model can fully handle being trained as a unified model (AR + NAR) or separate models (AR | NAR).
It's recommended to train as a unified model, then "distill" knowledge of each tasks separately, just in case.
"""
from .base_v2 import Base_V2, list_to_tensor, Categorical
from ..config import cfg

import torch
from torch.nn.utils.rnn import pad_sequence

import random
import math
import time
from einops import rearrange
from torch import Tensor
from tqdm import trange, tqdm

import logging

_logger = logging.getLogger(__name__)

from ..emb.qnt import trim, get_silence
from ..utils import get_devices, setup_logging, timer, clamp, convert_kwargs

from .lora import enable_lora
from ..samplers import cfg_logits

text_task = [ "stt", "phn", "un-phn" ]

class AR_NAR_V2(Base_V2):
	# yikes
	def forward_super(self, *args, **kwargs):
		return super().forward(*args, **kwargs)

	# parse inputs for training
	# a lot of this could be delegated back to the dataloader, but it's just easier to keep the task of the dataloader to provide sufficient data, and the model to process the data for training
	def forward_train(
		self,
		task_list: list[Tensor] | None = None,
		
		phns_list: list[Tensor] | None = None,
		proms_list: list[Tensor] | None = None,
		resps_list: list[Tensor] | None = None,
		
		lang_list: list[Tensor] | None = None,
		tone_list: list[Tensor] | None = None,
		len_list: list[Tensor] | None = None,
		text_list: list[Tensor] | None = None,
	):
		# deduce batch_size
		if phns_list:
			device = phns_list[0].device
			batch_size = len(phns_list)
		elif text_list:
			device = text_list[0].device
			batch_size = len(text_list)
		elif proms_list:
			device = proms_list[0].device
			batch_size = len(proms_list)
		elif resps_list:
			device = resps_list[0].device
			batch_size = len(resps_list)

		# specifies how to sample probabilities of which RVQ levels to train against
		rvq_levels_p = self.config.experimental.rvq_levels_p if self.config is not None else "equal"
		# determines which RVQ level to target per batch
		quant_level_range = self.config.experimental.rvq_level_range if self.config is not None and self.config.experimental.rvq_level_range else [ 0 if self.causal else 1, self.n_resp_levels - 1 ]
		# rate to perform token dropout errors
		token_dropout_error = self.config.experimental.token_dropout_error
		# RVQ levels to apply token dropout on
		token_dropout_rvq_levels = self.config.experimental.token_dropout_rvq_levels
		# RVQ levels to apply masking training on
		masking_train_rvq_levels = [0,self.n_resp_levels] # self.config.experimental.masking_train_rvq_levels
			
		if cfg.audio_backend == "nemo":
			rvq_levels_p = [ i for i in range( quant_level_range[0], quant_level_range[1] + 1 ) ]

		# CFG
		cfg_text_dropout_p = self.config.experimental.cfg_text_dropout_p if self.config is not None else 0.0
		cfg_cond_dropout_p = self.config.experimental.cfg_cond_dropout_p if self.config is not None else 0.0
		cfg_prom_dropout_p = self.config.experimental.cfg_prom_dropout_p if self.config is not None else 0.0
		use_raw_text_p = self.config.experimental.use_raw_text_p if self.config is not None else 0.0
		# rate to train RVQ level AR-ly or NAR-ly
		masking_train_p = self.config.experimental.masking_train_p if self.config is not None else 0.5
		masking_ratio = self.config.experimental.masking_ratio if self.config is not None else "random"
		# force set mask training
		if "len" not in self.capabilities:
			masking_train_p = 0.0
		elif "ar" not in self.capabilities:
			masking_train_p = 1.0
		# implicitly set it to all levels
		if not token_dropout_rvq_levels:
			token_dropout_rvq_levels = [0, self.resp_levels - 1]
		if not token_dropout_rvq_levels:
			token_dropout_rvq_levels = [0, 0]

		# allow passing a specific distribution of RVQ levels
		rvq_levels_p = rvq_levels_p if isinstance(rvq_levels_p, list) else []
		if not rvq_levels_p:
			lo, hi = quant_level_range[0], quant_level_range[1] + 1
			# randomly select a target RVQ-bin level (0 being AR, 1+ being NAR)
			if rvq_levels_p == "equal":
				rvq_levels_p = [ i for i in range( lo, hi ) ]
			elif rvq_levels_p == "normal":
				# yuck
				rvq_levels_p = [
					0,
					1, 1,
					2, 2, 2, 2,
					3, 3, 3, 3, 3, 3, 3, 3,
					4, 4, 4, 4, 4, 4, 4, 4,
					5, 5, 5, 5,
					6, 6,
					7,
				]
			else:
				rvq_levels_p = sum([[i for _ in range(hi - i)] for i in range( lo, hi ) ], [])

		# input RVQ levels
		quant_levels = [ random.choice( rvq_levels_p ) for i in range(batch_size) ]
		# timestep levels (for TTS NAR)
		timesteps = [ None for _ in range(batch_size) ]

		for i, task in enumerate( task_list ):
			lo, hi = masking_train_rvq_levels[0], masking_train_rvq_levels[1]
			if task in text_task:
				quant_levels[i] = 0 # self.n_resp_levels - 1
			elif lo <= quant_levels[i] and quant_levels[i] <= hi and random.random() < masking_train_p:
				# to-do: prioritize lower timesteps over later timesteps
				# ...except that the masking rate is still tied to the cosine scheduling, which does this already
				#r = random.random()
				#p = math.acos(r) / (math.pi * 0.5)
				#timesteps[i] = 1.0 - clamp(p, 0.0, 1.0)
				timesteps[i] = random.random()
				
				# instead make it between [0.2, 0.8]
				if masking_ratio == "rand":
					timesteps[i] = (timesteps[i] * 0.6) + 0.2

		# tensor to cat for RVQ level 0
		text_stop_sequence = torch.tensor([2], device=device, dtype=torch.int16)
		text_start_stop_sequence = torch.tensor([1, 2], device=device, dtype=torch.int16)
		audio_stop_sequence = torch.tensor([[self.stop_token]], device=device, dtype=torch.int16)

		# final validations and stuff
		for i, quant_level, resps, proms, task in zip(range(batch_size), quant_levels, resps_list, proms_list, task_list):
			# cap quant_level if it exceeds its corresponding resp/prom
			# this was needed for when my DAC-encoded audio was erroneously trimmed to 8 RVQ levels instead of 9
			if quant_level >= resps.shape[-1]:
				quant_levels[i] = resps.shape[-1] - 1

			# proms could be a Tensor, list[Tensor], or None
			if isinstance( proms, torch.Tensor ):
				if quant_level >= proms.shape[-1]:
					quant_levels[i] = proms.shape[-1] - 1

			elif isinstance( proms, list ):
				for j, prom in enumerate( proms ):
					if not isinstance( prom, torch.Tensor ):
						continue
					if quant_level >= prom.shape[-1]:
						quant_levels[i] = prom.shape[-1] - 1

			# apply token dropout error compensation
			"""
			if token_dropout_error > 0 and (token_dropout_rvq_levels[0] <= quant_level and quant_level <= token_dropout_rvq_levels[1]):
				steps = resps.shape[0]
				for l in range( quant_level ):
					for t in range( steps ):
						token = resps[t, l].item()

						if random.random() < token_dropout_error:								
							offset = 1 * ( 1 if random.random() < 0.5  else -1 )
							resps_list[i][t, l] = clamp(token + offset, 1, 1022) # +- 1
			"""

			# only apply stop token for RVQ level 0
			if timesteps[i] is None or (self.predict_causally):
				# append stop tokens for AR
				if task not in text_task:
					resps_list[i] = torch.cat([ resps, audio_stop_sequence.repeat((1, resps.shape[-1])) ])

			if task == "len":
				quant_levels[i] = 0

			# apply CFG (should probably only apply to NAR quant level 0)
			if task not in text_task + ["len"]:
				drop_text = False
				drop_audio = False
				swap_text = False

				if random.random() < cfg_prom_dropout_p:
					drop_audio = True
				
				if random.random() < cfg_cond_dropout_p:
					drop_audio = True
					drop_text = True
				
				if random.random() < use_raw_text_p and text_list[i] is not None:
					swap_text = True

				if drop_text:
					phns_list[i] = text_start_stop_sequence

				if drop_audio:
					proms_list[i] = None

				if swap_text and not drop_text:
					phns_list[i] = None

		inputs = self.inputs(
			phns_list=phns_list,
			proms_list=proms_list,
			resps_list=resps_list,
			lang_list=lang_list,
			tone_list=tone_list,
			task_list=task_list,
			text_list=text_list,
			time_list=timesteps,

			quant_levels=quant_levels,
		)

		return super().forward(
			inputs=inputs,
			quant_levels=quant_levels,
		)

	# handles doing demasking inferencing in parallel to inference all tokens
	# it works if the underlying model is trained properly (which is a pain)
	def forward_nar_masked(
		self,

		task_list: list[Tensor] | None = None,
		
		phns_list: list[Tensor] | None = None,
		proms_list: list[Tensor] | None = None,
		resps_list: list[Tensor] | None = None,
		
		lang_list: list[Tensor] | None = None,
		tone_list: list[Tensor] | None = None,
		len_list: list[Tensor] | None = None,
		text_list: list[Tensor] | None = None,

		disable_tqdm=False,
		use_lora=None,
		**sampling_kwargs,
	):
		device = phns_list[0].device
		batch_size = len(phns_list)

		level = 0
		if cfg.lora is not None:
			enable_lora( self, cfg.lora.active_level( level ) if use_lora is None else use_lora )

		# convert (N)AR specific args
		sampling_kwargs = convert_kwargs( sampling_kwargs, "ar_" )

		min_length = sampling_kwargs.pop("min_duration", 1)
		max_length = sampling_kwargs.pop("max_duration", 500)
		max_steps = sampling_kwargs.get("max_steps", 25)
		refine_on_stop = sampling_kwargs.get("refine_on_stop", False)
		entropix_sampling = sampling_kwargs.get("entropix_sampling", False)
		annealed_sampling = sampling_kwargs.get("annealed_sampling", True)

		# greedy sampling is very, very much preferred, but using greedy logit scores later helps enough
		temperature = sampling_kwargs.pop("temperature", 0.0)
		minimum_cfg_strength = sampling_kwargs.get("minimum_cfg_strength", 2.5)
		# this really helps keep audio coherent so far
		cfg_strength = sampling_kwargs.get("cfg_strength", minimum_cfg_strength)
		cfg_rescale = sampling_kwargs.pop("cfg_rescale", 0.75)
		start_noise = sampling_kwargs.get("denoise_start", 0.0)
		end_noise = sampling_kwargs.get("denoise_end", 1.0)
		remasking = sampling_kwargs.get("remasking", True)
		max_steps = math.floor(max_steps * (end_noise - start_noise))

		# to specify the initial mask used
		vc_list = sampling_kwargs.pop("vc_list", None)
		vc_threshold = sampling_kwargs.pop("vc_threshold", 0.25)
		vc_mask_p = sampling_kwargs.pop("vc_mask_p", 0.25)

		len_list = [ clamp(l, min_length, max_length) for l in len_list ]
		
		# force set CFG because too low / no CFG causes issues
		original_cfg_strength = cfg_strength
		cfg_strength = max( cfg_strength, minimum_cfg_strength )

		prefix_context = sampling_kwargs.get("prefix_context", None)
		# fill with masked tokens (even though they get masked anyways)
		resps_list = [ torch.ones((seq_len, self.n_resp_levels), dtype=torch.int16, device=device) * self.mask_token for seq_len in len_list ]
		# fill scores
		scores = [ torch.ones((seq_len), dtype=torch.float32, device=device) for seq_len in len_list ]

		quant_levels = [ level for _ in range(batch_size) ]
		null_text = [ torch.tensor([1, 2], device=device, dtype=torch.int16) for _ in range(batch_size) ]
		null_prom = [ None for _ in range(batch_size) ]

		iterator = tqdm(torch.linspace(start_noise, end_noise, max_steps), desc="NAR Masked", disable=disable_tqdm)
		for timestep in iterator:
			# update previous list of tokens
			prev_list = resps_list
			# ramp down over time
			annealing = 1.0 - timestep
			# get noise level, per cosine scheduling
			noise_p = math.cos( timestep * math.pi * 0.5 )
			# proportion of tokens to remask
			remask_p = 1.0 / (max_steps * 2) if remasking else 0
			# pick the worst scoring tokens to mask off
			masked_indices = [ score.topk( clamp( int( noise_p * seq_len + remask_p * seq_len ), 1, seq_len), dim=-1 ).indices for score, seq_len in zip(scores, len_list) ]
			# normal masking
			# mask off inputs
			resps_list = [ torch.stack([resp[:, l].scatter(0, indices, self.mask_token) for l in range(self.n_resp_levels)], dim=-1) for resp, indices in zip( resps_list, masked_indices ) ]
			# boolean mask
			is_masked = [ resps == self.mask_token for resps in resps_list ]
			# timestep inputs
			time_list = [ timestep for _ in range(batch_size) ]

			sampling_temperature = temperature * annealing if annealed_sampling else temperature
			sampling_cfg = cfg_strength * timestep if annealed_sampling else cfg_strength

			input_resps_list = resps_list

			# setup inputs
			inputs = super().inputs(
				phns_list=phns_list,
				proms_list=proms_list,
				resps_list=input_resps_list,
				lang_list=lang_list,
				tone_list=tone_list,
				time_list=time_list,
				quant_levels=quant_levels,
			)
			output = super().forward(
				inputs=inputs,
				quant_levels=quant_levels,
			)

			logits = output.logits
			if cfg_strength > 0:
				null_inputs = super().inputs(
					phns_list=null_text,
					proms_list=null_prom,
					resps_list=input_resps_list,
					lang_list=lang_list,
					tone_list=tone_list,
					time_list=time_list,
					quant_levels=quant_levels,
				)
				null_output = super().forward(
					inputs=null_inputs,
					quant_levels=quant_levels,
				)

				logits = cfg_logits( logits=output.logits, null=null_output.logits, strength=cfg_strength, rescale=cfg_rescale, lens=[ l for l in len_list ] )

			l_scores = []
			l_resps_list = []
			# cringe hack because we're able to sample multiple levels at once
			for l in range(self.n_resp_levels):
				# sample with sampler settings
				filtered_sampled = super().sample(
					logits=[ logit[l] for logit in logits ],
					prev_list=[ resp[..., l] for resp in prev_list ],
					quant_levels=quant_levels,

					temperature=sampling_temperature,
					**sampling_kwargs,
				)

				# retrieves unfiltered logits
				unfiltered_sampled = super().sample(
					logits=[ logit[l] for logit in logits ],
					prev_list=[ resp[..., l] for resp in prev_list ],
					quant_levels=quant_levels,

					temperature=0.0,
					**sampling_kwargs,
				)

				# get sampled tokens
				sampled_ids = filtered_sampled.ids
				# keep unmasked tokens
				l_resps_list.append([ torch.where( masked[..., l], input_ids, resps[..., l] ).to(torch.int16) for masked, input_ids, resps in zip( is_masked, sampled_ids, resps_list ) ])
				# get probability scores
				l_scores.append([ 
					# conjugate to have worse scoring tokens picked for topk
					1.0 - 
						# only keep scores of tokens we are predicting (and ignore the tokens previously finalized)
						torch.where( masked[..., l], torch.tensor([score for index, score in enumerate(scores)], device=device), torch.ones(masked[..., l].shape, device=device) )
					# use unmodified logit scores for this, as it offers better stability
					for scores, masked in zip( unfiltered_sampled.scores, is_masked )
				])

			resps_list = []
			scores = []

			for batch_index in range(batch_size):
				score = sum([ l_scores[level][batch_index] for level in range(self.n_resp_levels) ]) / self.n_resp_levels
				resp = torch.stack([ l_resps_list[level][batch_index] for level in range(self.n_resp_levels) ], dim=-1)

				scores.append( score )
				resps_list.append( resp )

		return resps_list

	def forward_ar_len(
		self,

		task_list: list[Tensor],

		phns_list: list[Tensor] | None = None,
		text_list: list[Tensor] | None = None,
		proms_list: list[Tensor] | None = None,
		resps_list: list[Tensor] | None = None,
		lang_list: list[Tensor] | None = None,
		tone_list: list[Tensor] | None = None,
		len_list: list[Tensor] | None = None,

		disable_tqdm=False,
		use_lora=None,
		**sampling_kwargs,
	):
		# deduce batch_size
		if phns_list:
			device = phns_list[0].device
			batch_size = len(phns_list)
		elif text_list:
			device = text_list[0].device
			batch_size = len(text_list)
		elif proms_list:
			device = proms_list[0].device
			batch_size = len(proms_list)
		elif resps_list:
			device = resps_list[0].device
			batch_size = len(resps_list)

		if cfg.lora is not None:
			enable_lora( self, cfg.lora.active_level( 0 ) if use_lora is None else use_lora )

		# convert AR specific args
		sampling_kwargs = convert_kwargs( sampling_kwargs, "ar_" )

		temperature = sampling_kwargs.get("temperature", 1.0)
		cfg_strength = sampling_kwargs.get("cfg_strength", 0.0)
		cfg_rescale = sampling_kwargs.pop("cfg_rescale", 0.7)
		min_temperature = sampling_kwargs.get("min_temperature", -1.0)
		max_duration = sampling_kwargs.get("max_duration", 500)
		beam_width = sampling_kwargs.get("beam_width", 0)
		entropix_sampling = sampling_kwargs.get("entropix_sampling", False)
		refine_on_stop = sampling_kwargs.get("refine_on_stop", False)
		input_prompt_prefix = sampling_kwargs.get("input_prompt_prefix", False)
		layer_skip = sampling_kwargs.get("layer_skip", False)
		prefix_silence = sampling_kwargs.get("prefix_silence", 0.0)
		mirostat_tau = sampling_kwargs.get("mirostat_tau", 0.0)
		mirostat_eta = sampling_kwargs.get("mirostat_eta", 0.0)

		# inference len
		sequence_list = [ torch.tensor([0], device=device,dtype=torch.int16) for _ in range(batch_size) ]
		stopped = torch.zeros(batch_size, device=device).bool()
		
		stop_token = 10
		task_list = [ "len" for _ in range(batch_size) ]
		quant_levels = [ 0 for _ in range( max( batch_size, beam_width ) ) ]

		iterator = trange(10, desc="AR", disable=disable_tqdm)
		for n in iterator:
			len_list = sequence_list

			inputs = self.inputs(
				task_list=task_list,
				
				phns_list=phns_list,
				proms_list=proms_list,
				resps_list=resps_list,
				
				lang_list=lang_list,
				tone_list=tone_list,
				len_list=len_list,
				text_list=text_list,
				
				quant_levels=quant_levels,
			)

			output = super().forward(
				inputs=inputs,
				quant_levels=quant_levels,
			)
			logits = output.logits

			r = [ logit[-1:].argmax(dim=1) for logit in logits ]
			# sanitize
			for i, token in enumerate(r):
				if token > stop_token:
					r[i][0] = stop_token

			# append tokens
			for i, ri in enumerate(r):
				if stop_token in ri:
					stopped[i] = True
				sequence_list[i] = torch.cat([sequence_list[i], ri.to(device)])

			# stop token found
			stopped |= r == stop_token
			if stopped.all().item():
				iterator.close()
				break

		# convert tokens into int
		return [ int("".join([ str(token.item()) for token in r if token != stop_token ])) for r in sequence_list ]

	def forward_ar(
		self,

		task_list: list[Tensor],

		phns_list: list[Tensor] | None = None,
		text_list: list[Tensor] | None = None,
		proms_list: list[Tensor] | None = None,
		resps_list: list[Tensor] | None = None,
		lang_list: list[Tensor] | None = None,
		tone_list: list[Tensor] | None = None,
		len_list: list[Tensor] | None = None,

		disable_tqdm=False,
		use_lora=None,
		**sampling_kwargs,
	):
		# deduce batch_size
		if phns_list:
			device = phns_list[0].device
			batch_size = len(phns_list)
		elif text_list:
			device = text_list[0].device
			batch_size = len(text_list)
		elif proms_list:
			device = proms_list[0].device
			batch_size = len(proms_list)
		elif resps_list:
			device = resps_list[0].device
			batch_size = len(resps_list)

		if cfg.lora is not None:
			enable_lora( self, cfg.lora.active_level( 0 ) if use_lora is None else use_lora )

		# convert AR specific args
		sampling_kwargs = convert_kwargs( sampling_kwargs, "ar_" )

		temperature = sampling_kwargs.get("temperature", 1.0)
		cfg_strength = sampling_kwargs.get("cfg_strength", 0.0)
		cfg_rescale = sampling_kwargs.pop("cfg_rescale", 0.7)
		min_temperature = sampling_kwargs.get("min_temperature", -1.0)
		max_duration = sampling_kwargs.get("max_duration", 500)
		beam_width = sampling_kwargs.get("beam_width", 0)
		entropix_sampling = sampling_kwargs.get("entropix_sampling", False)
		refine_on_stop = sampling_kwargs.get("refine_on_stop", False)
		input_prompt_prefix = sampling_kwargs.get("input_prompt_prefix", False)
		layer_skip = sampling_kwargs.get("layer_skip", False)
		prefix_silence = sampling_kwargs.get("prefix_silence", 0.0)
		mirostat_tau = sampling_kwargs.get("mirostat_tau", 0.0)
		mirostat_eta = sampling_kwargs.get("mirostat_eta", 0.0)

		start_slice = [ 0 for _ in range(batch_size) ]
		sequence_list = [ torch.zeros((0, 8), device=device).to(torch.int16) for _ in range(batch_size) ]
		stopped = torch.zeros(batch_size, device=device).bool()
		
		audio_stop_token = self.stop_token
		text_stop_token = 2

		state = None
		mirostat = [
			{"n": 1024, "tau": mirostat_tau, "eta": mirostat_eta, "max_surprise": mirostat_eta * 2, "error_surprise": 0, "running_total_surprise": 0}
		] * batch_size if mirostat_tau > 0.0 else None

		scores = [ 1.0 ] * beam_width
		metrics = []

		null_text = [ torch.tensor([1, 2], device=device, dtype=torch.int16) for _ in range(batch_size) ]
		null_prom = [ None for _ in range(batch_size) ]

		# get next in sequence
		iterator = trange(max_duration // max(1, self.causal_size), desc="AR", disable=disable_tqdm)
		for n in iterator:
			if text_list is not None:
				text_list = [ sequence_list[i] if task in text_task else text_list[i] for i, task in enumerate(task_list) ]
			else:
				phns_list = [ sequence_list[i] if task in text_task else phns_list[i] for i, task in enumerate(task_list) ]
			resps_list = [ sequence_list[i] if task not in text_task else resps_list[i] for i, task in enumerate(task_list) ]

			quant_levels = [ 0 for _ in range( max( batch_size, beam_width ) ) ]

			inputs = self.inputs(
				task_list=task_list,
				
				phns_list=phns_list,
				proms_list=proms_list,
				resps_list=resps_list,
				
				lang_list=lang_list,
				tone_list=tone_list,
				len_list=len_list,
				text_list=text_list,
				
				quant_levels=quant_levels,
			)

			# to-do: find an elegant way to write this
			output = super().forward(
				inputs=inputs,
				state=state,
				#layer_skip_variables=sampling_layer_skip_variables,
				output_attentions=entropix_sampling,
			)

			if cfg_strength > 0:
				null_inputs = super().inputs(
					phns_list=null_text,
					proms_list=null_prom,
					resps_list=resps_list,
					lang_list=lang_list,
					tone_list=tone_list,
					quant_levels=quant_levels,
				)
				null_output = super().forward(
					inputs=null_inputs,
					quant_levels=quant_levels,
					#layer_skip_variables=sampling_layer_skip_variables,
				)
				logits = cfg_logits( logits=output.logits, null=null_output.logits, strength=cfg_strength, rescale=cfg_rescale, lens=[ resp.shape[0] + 1 for resp in resps_list ] )
			
			logits, state = output.logits, output.state

			l_resps_list = [ [] for _ in range(batch_size) ]
			for l in range(self.n_resp_levels):
				sampled = super().sample(
					logits=[ logit[l] for logit in logits ],
					#prev_list=[ resp[..., l] for resp in resps_list ],
					**(sampling_kwargs | {"attentions": output.attentions if entropix_sampling else None}),
				)

				ids = sampled.ids

				# append tokens
				for i, token in enumerate(ids):
					if audio_stop_token in token:
						stopped[i] = True
					l_resps_list[i].append(token.to(device))

			for i, l in enumerate(l_resps_list):
				sequence_list[i] = torch.cat([sequence_list[i], torch.stack(l, dim=-1)])

			# stop token found
			# stopped |= r == stop_token
			if stopped.all().item():
				iterator.close()
				break

		for i, l in enumerate( sequence_list ):
			index = (l == audio_stop_token).nonzero()
			# kludge for when it doesnt actually hit a stop token but i cant be bothered to properly address it right now since it only came up in test training at the moment
			try:
				index = index[:, 0].min()
				sequence_list[i] = sequence_list[i][:index]
			except Exception as e:
				pass

		return sequence_list

	def forward(
		self,
		task_list: list[Tensor] | None = None,

		phns_list: list[Tensor] | None = None,
		proms_list: list[Tensor] | None = None,
		resps_list: list[Tensor] | None = None,
		
		lang_list: list[Tensor] | None = None,
		tone_list: list[Tensor] | None = None,
		len_list: list[Tensor] | None = None,
		text_list: list[Tensor] | None = None,

		training: bool | None = None,

		disable_tqdm=False,
		use_lora=None,
		**sampling_kwargs,
	):
		# deduce batch_size
		# deduce batch_size
		if phns_list:
			device = phns_list[0].device
			batch_size = len(phns_list)
		elif text_list:
			device = text_list[0].device
			batch_size = len(text_list)
		elif proms_list:
			device = proms_list[0].device
			batch_size = len(proms_list)
		elif resps_list:
			device = resps_list[0].device
			batch_size = len(resps_list)

		# implicitly set for training
		if training is None and phns_list is not None and resps_list is not None:
			n_levels_set = {r.shape[-1] for r in resps_list}
			n_levels = next(iter(n_levels_set))

			training = n_levels == self.n_resp_levels

		# is training
		if training:
			return self.forward_train(
				task_list=task_list,
				
				phns_list=phns_list,
				proms_list=proms_list,
				resps_list=resps_list,
				
				lang_list=lang_list,
				tone_list=tone_list,
				len_list=len_list,
				text_list=text_list,
			)

		# is NAR
		if (len_list is not None or resps_list is not None) and phns_list is not None:
			return self.forward_nar_masked(
				task_list=task_list,

				phns_list=phns_list,
				proms_list=proms_list,
				resps_list=resps_list,
				
				lang_list=lang_list,
				tone_list=tone_list,
				len_list=len_list,
				text_list=text_list,

				disable_tqdm=disable_tqdm,
				use_lora=use_lora,
				**sampling_kwargs,
			)

			# NAR demasking for all levels
			"""
			resps_lists = [ None for _ in range(batch_size) ]
			for level in range(self.n_resp_levels):
				resp_list = self.forward_nar_masked(
					task_list=task_list,

					phns_list=phns_list,
					proms_list=proms_list,
					resps_list=resps_list,
					
					lang_list=lang_list,
					tone_list=tone_list,
					len_list=len_list,
					text_list=text_list,

					disable_tqdm=disable_tqdm,
					use_lora=use_lora,
					quant_levels=[ level for _ in range(batch_size) ],
					**sampling_kwargs,
				)

				for batch_index, resp in enumerate(resp_list):
					if resps_lists[batch_index] is None:
						resps_lists[batch_index] = []
					
					resps_lists[batch_index].append( resp )

			for batch_index, resps in enumerate(resps_lists):
				resps_lists[batch_index] = torch.stack( resps, dim=-1 )

			return resps_lists
			"""

		if task_list is not None and task_list[0] == "len":
			return self.forward_ar_len(
				task_list=task_list,
				
				phns_list=phns_list,
				proms_list=proms_list,
				resps_list=resps_list,
				
				lang_list=lang_list,
				tone_list=tone_list,
				len_list=len_list,
				text_list=text_list,

				disable_tqdm=disable_tqdm,
				use_lora=use_lora,
				**sampling_kwargs,
			)

		# is AR
		return self.forward_ar(
			task_list=task_list,
			
			phns_list=phns_list,
			proms_list=proms_list,
			resps_list=resps_list,
			
			lang_list=lang_list,
			tone_list=tone_list,
			len_list=len_list,
			text_list=text_list,

			disable_tqdm=disable_tqdm,
			use_lora=use_lora,
			**sampling_kwargs,
		)


def example_usage():
	#cfg.device = "cuda"
	#cfg.trainer.backend = "local"

	from functools import partial
	from einops import repeat
	from tqdm import tqdm

	from ..emb.qnt import decode_to_file, unload_model, trim_random, repeat_extend_audio, concat_audio, merge_audio
	from ..data import _load_artifact
	from ..engines import Engine, Engines
	from ..utils import ml
	from ..utils import setup_logging
	
	import numpy as np
	import re
	
	# cfg.model.experimental.masking_train_p = 0.5
	cfg.hyperparameters.batch_size = 1
	cfg.hyperparameters.gradient_accumulation_steps = 1

	setup_logging()

	def load_artifact( path ):
		audio, metadata = _load_artifact(path, return_metadata=True)

		audio = audio.to(cfg.device)
		text = torch.tensor( cfg.tokenizer.encode( metadata["phonemes"] ) ).to(dtype=torch.uint8, device=cfg.device)

		return text, audio

	text, audio = load_artifact(f"./data/qnt.{cfg.audio_backend_extension}")
	batch_size = cfg.hyperparameters.batch_size

	phns_list = [ text ] * batch_size
	proms_list = [ audio[:int(cfg.dataset.frames_per_second), :] ] * batch_size
	resps_list = [ audio[:int(cfg.dataset.frames_per_second * 4), :] ] * batch_size

	kwargs = {
		'n_audio_tokens': cfg.model.audio_tokens,

		'd_model': cfg.model.dim,
		'd_ffn': cfg.model.ffn,
		'n_heads': cfg.model.heads,
		'n_layers': cfg.model.layers,
		'n_experts': cfg.model.experts,
		'p_dropout': 0.1,

		'config': cfg.model
	}

	bos_id, space_id, eos_id = cfg.tokenizer.encode( " " )
	
	available_tasks = [] + (["tts-ar"] if "ar" in cfg.model.capabilities else []) + (["tts-nar"] if "len" in cfg.model.capabilities else [])

	if cfg.model.experimental.masking_train_p == 0:
		available_tasks = ["tts-ar"]
	elif cfg.model.experimental.masking_train_p == 1:
		available_tasks = ["tts-nar"]

	model = AR_NAR_V2(**kwargs).to(cfg.device)
	steps = 250 // batch_size

	optimizer = cfg.hyperparameters.optimizer.lower() if cfg.yaml_path is not None else "prodigy"
	scheduler = cfg.hyperparameters.scheduler.lower() if cfg.yaml_path is not None else ""
	learning_rate = cfg.hyperparameters.learning_rate if cfg.yaml_path is not None else None

	params = {
		"params": model.parameters()
	}
	if cfg.optimizations.dadaptation:
		# do not combine the two
		if scheduler == "schedulefree":
			scheduler = ""

		learning_rate = 1.0
	
	if optimizer == "prodigy":
		if learning_rate is None:
			learning_rate = 1.0

		optimizer = ml.Prodigy
	elif optimizer == "adagrad":
		if learning_rate is None:
			learning_rate = 1.0e-2

		optimizer = ml.Adagrad
	elif optimizer == "adamw":
		if learning_rate is None:
			learning_rate = 1.0e-4

		optimizer = ml.AdamW
	elif optimizer == "sdg":
		if learning_rate is None:
			learning_rate = 1.0e-4

		optimizer = ml.SGD
	elif optimizer == "apollo":
		if learning_rate is None:
			learning_rate = 0.01

		optimizer = ml.Apollo
		params["params"] = [
			{'params': params, 'rank': 1, 'proj': 'random', 'scale_type': 'tensor', 'scale': 128,'update_proj_gap': 200, 'proj_type': 'std'}
		]
	elif optimizer == "muon":
		optimizer = ml.Muon

		muon_params = [ param for name, param in model.model.named_parameters() if param.ndim >= 2 ]
		adamw_params = [ param for name, param in model.model.named_parameters() if param.ndim < 2 ]
		adamw_params += [ param for name, param in model.named_parameters() if not name.startswith('model.') ]

		params["params"] = [
			{ "params": muon_params, "muon": True },
			{ "params": adamw_params, "muon": False, "betas": (0.95, 0.95), "eps": 1e-8 },
		]
	elif optimizer == "cosmos":
		optimizer = ml.COSMOS
	else:
		raise ValueError(f"Unrecognized optimizer: {optimizer}")

	_logger.info(f"Optimizer: {optimizer}\tLearning rate: {learning_rate}")

	params["lr"] = learning_rate
	optimizer = optimizer(**params)

	if scheduler == "schedulefree":
		if isinstance(optimizer, ml.AdamW):
			scheduler = ml.schedulefree.AdamWScheduleFree
		elif isinstance(optimizer, ml.SGD):
			scheduler = ml.schedulefree.SGDScheduleFree
		else:
			scheduler = None

		if scheduler is not None:
			_logger.info(f"Scheduler: {scheduler}")
			optimizer = scheduler( model.parameters(), lr = learning_rate )

	if cfg.optimizations.replace and cfg.optimizations.linear:
		model = ml.replace_linear( model )
		
	if cfg.optimizations.replace and cfg.optimizations.embedding:
		model = ml.replace_embedding( model )

	"""
	cfg.optimizations.model_offloading = {
		"devices": ["cuda:0", "cpu"],
	#	"limits": [ 0.9, -1 ],
		"assign": [[ f'layers.{i}.' for i in range(0,10) ], [ f'layers.{i}.' for i in range(11,12) ] + [ "model.norm" ]],
	#	"limits": [ 256 * (1024 ** 2), -1 ]
	}
	"""
	
	engine = Engine(model=model, optimizer=optimizer)
	engines = Engines({"ar+nar": engine})
	engines.setup()
	
	"""
	if cfg.optimizations.model_offloading:
		model = ml.offload_model( model, policy=cfg.optimizations.model_offloading )
	"""

	"""
	torch.save( {
		'module': model.state_dict()
	}, f"./data/{cfg.model.arch_type}.pth" )
	"""

	_logger.info(f"AR+NAR ({cfg.model.arch_type}, {cfg.audio_backend}) parameter count: {sum(p.numel() for p in model.parameters() if p.requires_grad)}")

	@torch.no_grad()
	def sample_data(t=None):
		if isinstance(t, list):
			tasks = t
			texts = [ phns_list[0].to(cfg.device) if task not in text_task else None for i, task in enumerate( tasks ) ]
			proms = [ proms_list[0].to(cfg.device) if task not in text_task else [ "stt" ] for i, task in enumerate( tasks ) ]
			resps = [ None if task not in text_task else resps_list[0].to(cfg.device) for i, task in enumerate( tasks ) ]

			return texts, proms, resps, tasks

		texts = []
		proms = []
		resps = []
		tasks = []

		for i in range(batch_size):
			task = random.choice(available_tasks) if t is None else t

			text = phns_list[i].to(cfg.device)
			prom = proms_list[i].to(cfg.device)
			resp = resps_list[i].to(cfg.device)

			# do nothing
			if task == "stt":
				prom = [ task ]
			else:
				task = "tts" if random.random() > 0.1 or "len" not in cfg.model.capabilities else "len"

			texts.append( text )
			proms.append( prom )
			resps.append( resp )
			tasks.append( task )

		return texts, proms, resps, tasks

	@torch.inference_mode()
	def sample( name, steps=500, task=None ):
		engine.eval()

		phns_list, proms_list, resp_list, task_list = sample_data( task )

		if task == "tts-nar":
			# len_list = engine( phns_list=phns_list, proms_list=proms_list, task_list=["len"], max_steps=5, temperature=0.0 )
			len_list = [ r.shape[0] for r in resp_list ]
			resps_list = engine( phns_list=phns_list, proms_list=proms_list, len_list=len_list )
		else:
			resps_list = engine( phns_list=phns_list, proms_list=proms_list, task_list=["tts"], max_duration=steps, temperature=1.0 )
			if resps_list[0].dim() == 1 or resps_list[0].shape[-1] == 1:
				resps_list = engine( phns_list=phns_list, proms_list=proms_list, resps_list=resps_list, temperature=0.0 )

		for i, o in enumerate(resps_list):
			print( o.shape, o )
			_ = decode_to_file(o.to(dtype=torch.int32), f"data/{cfg.model.arch_type}.{cfg.audio_backend}.{i}.{name}.{task}.wav", device=cfg.device)

		unload_model()

	def train():
		engine.train()
		t = trange(steps)
		for i in t:
			texts, proms, resps, tasks = sample_data()

			stats = {"step": i}
			with torch.autograd.set_detect_anomaly(cfg.trainer.detect_grad_anomaly):
				stats |= engine.traverse(phns_list=texts, proms_list=proms, resps_list=resps, task_list=tasks, training=True)
			stats |= {"grad_norm": engine.get_global_grad_norm()}

			tqdm.write(f"{stats}")

		"""
		torch.save( {
			'module': model.state_dict()
		}, f"./data/{cfg.model.arch_type}.pth" )
		"""

	task = available_tasks[0]
	#sample("init", task=task)

	train()

	"""
	if cfg.optimizations.compile:
		model = ml.compile_model(model, backend=cfg.optimizations.compile)
	"""
	
	for task in available_tasks:
		sample("final", task=task)

	engines.quit()

if __name__ == "__main__":
	example_usage()