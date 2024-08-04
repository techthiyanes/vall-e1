"""
# an AR + NAR model that handles:
* inferencing the primary RVQ level in an autoregressive manner (AR)
* inferencing the remaining RVQ levels in parallel (NAR)

This model can fully handle being trained as a unified model (AR + NAR) or separate models (AR | NAR).
It's recommended to train as a unified model, then "distill" knowledge of each tasks separately, just in case.
"""
from .base import Base, list_to_tensor, Categorical
from ..config import cfg

import torch
from torch.nn.utils.rnn import pad_sequence

import random
import math
from einops import rearrange
from torch import Tensor
from tqdm import trange

from ..emb.qnt import trim, encode_as_embedding

from .lora import enable_lora

def clamp(n, lo, hi):
	return max(lo, min(n, hi))

class AR_NAR(Base):
	def forward(
		self,
		text_list: list[Tensor],
		proms_list: list[Tensor],
		resps_list: list[Tensor] | None = None,
		
		task_list: list[Tensor] | None = None,
		lang_list: list[Tensor] | None = None,
		tone_list: list[Tensor] | None = None,
		len_list: list[Tensor] | None = None,

		training: bool | None = None,

		max_steps: int = 1000,
		max_levels: int = 0,

		sampling_temperature: float = 1.0,
		sampling_min_temperature: float = -1.0,
		sampling_top_k: int = -100,
		sampling_top_p: float = 1.0,
		sampling_repetition_penalty: float = 1.0,
		sampling_repetition_penalty_decay: float = 0.0,
		sampling_length_penalty: float = 0.0,
		sampling_beam_width: int = 0,
		sampling_mirostat_tau: float = 0.0,
		sampling_mirostat_eta: float = 0.1,
		sampling_dry_multiplier=0.0,
		sampling_dry_base=1.75,
		sampling_dry_allowed_length=2,

		disable_tqdm=False,
	):
		device = text_list[0].device
		batch_size = len(text_list)
		
		# generate task list if not provided
		if task_list is None:
			task_list = [ "tts" for _ in range(batch_size) ]

		# is training or NAR
		if resps_list is not None:
			n_levels_set = {r.shape[-1] for r in resps_list}
			n_levels = next(iter(n_levels_set))

			if training is None:
				training = n_levels == self.n_resp_levels

			# is training
			if training:
				# specifies how to sample probabilities of which RVQ levels to train against
				p_rvq_levels = self.config.experimental.p_rvq_levels if self.config is not None else "equal"
				# determines which RVQ level to target per batch
				quant_level_range = self.config.experimental.rvq_level_range if self.config is not None and self.config.experimental.rvq_level_range else [ 0 if self.causal else 1, self.n_resp_levels - 1 ]
				# rate to perform token dropout errors
				token_dropout_error = self.config.experimental.token_dropout_error
				# RVQ levels to apply token dropout on
				token_dropout_rvq_levels = self.config.experimental.token_dropout_rvq_levels
				# implicitly set it to all levels
				if not token_dropout_rvq_levels:
					token_dropout_rvq_levels = [0, self.resp_levels - 1]
				# allow passing a specific distribution of RVQ levels
				p_rvq_levels = p_rvq_levels if isinstance(p_rvq_levels, list) else []
				if not p_rvq_levels:
					lo, hi = quant_level_range[0], quant_level_range[1] + 1
					# randomly select a target RVQ-bin level (0 being AR, 1+ being NAR)
					if p_rvq_levels == "equal":
						p_rvq_levels = [ i for i in range( lo, hi ) ]
					else:
						# yuck
						p_rvq_levels = sum([[i for _ in range(hi - i)] for i in range( lo, hi ) ], [])

				# input RVQ levels
				quant_levels = [ random.choice( p_rvq_levels ) for i in range(batch_size) ]
				# trim resps to only contain all levels below the target level
				resps_list = [r[..., :l+1] for r, l in zip(resps_list, quant_levels)]
				# tensor to cat for RVQ level 0
				stop_sequence = torch.Tensor([[self.stop_token] * 1]).to(device=device, dtype=torch.int16)
				# I hate python's value/reference semantics so much
				for i, quant_level, resps, proms in zip(range(batch_size), quant_levels, resps_list, proms_list):
					# cap quant_level if it exceeds its corresponding resp/prom
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
					if token_dropout_error > 0 and (token_dropout_rvq_levels[0] <= quant_level and quant_level <= token_dropout_rvq_levels[1]):
						steps = resps.shape[0]
						for l in range( quant_level ):
							for t in range( steps ):
								token = resps[t, l].item()

								if random.random() < token_dropout_error:								
									offset = 1 * ( 1 if random.random() < 0.5  else -1 )
									resps_list[i][t, l] = clamp(token + offset, 1, 1022) # +- 1

					# only apply stop token for RVQ level 0
					if quant_level <= 0:
						# append stop tokens for AR
						resps_list[i] = torch.cat([ resps, stop_sequence ])
					

				inputs = self.inputs(
					text_list=text_list,
					proms_list=proms_list,
					resps_list=resps_list,
					lang_list=lang_list,
					tone_list=tone_list,
					task_list=task_list,

					quant_levels=quant_levels,
				)

				return super().forward(
					inputs=inputs,
					quant_levels=quant_levels, # could technically just grab this from the above inputs since they're included as an RVQ level token
				)
			
			# is NAR
			if max_levels == 0:
				max_levels = self.n_max_levels - 1

			# expand if given a raw 1D tensor
			for i, resp in enumerate(resps_list):
				if resp.dim() == 1:
					resps_list[i] = resp.unsqueeze(-1)
			
			prev_list = resps_list

			for n in trange( max_levels, desc="NAR", disable=disable_tqdm ):				
				level = prev_list[0].shape[-1]
				if level >= max_levels + 1: # min(max_levels + 1, self.n_resp_levels): # commented out to experiment with exceeding trained levels
					break

				if cfg.lora is not None:
					enable_lora( self, cfg.lora.active_level( level ) )

				quant_levels = [ level for _ in range(batch_size) ] # torch.full((len(text_list),), level)

				inputs = self.inputs(
					text_list=text_list,
					proms_list=proms_list,
					resps_list=prev_list,
					lang_list=lang_list,
					tone_list=tone_list,
					quant_levels=quant_levels,
				)

				logits = super().forward(
					inputs=inputs,
					quant_levels=quant_levels,
				)

				resps_list = super().sample(
					logits=logits,
					resps_list=prev_list,
					quant_levels=quant_levels,

					temperature=sampling_temperature,
					min_temperature=sampling_min_temperature,
					top_p=sampling_top_p,
					top_k=sampling_top_k,
					#repetition_penalty=sampling_repetition_penalty,
					#repetition_penalty_decay=sampling_repetition_penalty_decay,
					#length_penalty=sampling_length_penalty,
					#beam_width=sampling_beam_width,
					#mirostat=mirostat,
				)

				prev_list = [ torch.cat([rs, r.unsqueeze(-1).to(device)], dim=-1) for rs, r in zip(prev_list, resps_list) ]

			if cfg.lora is not None:
				enable_lora( self )

			return prev_list
		
		# is AR
		if cfg.lora is not None:
			enable_lora( self, cfg.lora.active_level( 0 ) )

		sequence_list = [ torch.zeros(0, device=device).to(torch.int16) for _ in range(batch_size) ]
		stopped = torch.zeros(batch_size, device=device).bool()
		
		stop_token = self.stop_token


		state = None
		mirostat = [
			{"n": 1024, "tau": sampling_mirostat_tau, "eta": sampling_mirostat_eta, "max_surprise": sampling_mirostat_eta * 2, "error_surprise": 0, "running_total_surprise": 0}
		] * batch_size if sampling_mirostat_tau > 0.0 else None

		scores = [ 1.0 ] * sampling_beam_width

		# get next in sequence
		for n in trange(max_steps // max(1, self.causal_size), desc="AR", disable=disable_tqdm):
			resps_list = [x.unsqueeze(dim=-1) for x in sequence_list]

			inputs = self.inputs(
				text_list=text_list,
				proms_list=proms_list,
				resps_list=resps_list,
				lang_list=lang_list,
				tone_list=tone_list,
				len_list=len_list,
				task_list=task_list,
				quant_levels=[ 0 for _ in range( max( batch_size, sampling_beam_width ) ) ]
			)

			if state is not None:
				logits, state = super().forward(
					inputs=inputs,
					state=state,
				)
			else:
				logits = super().forward(
					inputs=inputs,
					state=state,
				)

			r = super().sample(
				logits=logits,
				resps_list=resps_list,

				temperature=sampling_temperature,
				min_temperature=sampling_min_temperature,
				top_p=sampling_top_p,
				top_k=sampling_top_k,
				repetition_penalty=sampling_repetition_penalty,
				repetition_penalty_decay=sampling_repetition_penalty_decay,
				length_penalty=sampling_length_penalty,
				beam_width=sampling_beam_width,

				mirostat=mirostat,

				dry_multiplier=sampling_dry_multiplier,
				dry_base=sampling_dry_base,
				dry_allowed_length=sampling_dry_allowed_length,
			)

			if mirostat is not None:
				# r is the state
				mirostat = r
				# extract token from state
				r = [ state["token"] for state in mirostat ]
			# we do it here because the sampler will already expand our logits list
			elif sampling_beam_width > 0:
				# expand tuple
				r, s = r
				# first step, expand batch
				if batch_size == 1:
					batch_size = sampling_beam_width
					text_list = text_list * sampling_beam_width
					proms_list = proms_list * sampling_beam_width
					sequence_list = sequence_list * sampling_beam_width
					stopped = torch.zeros(batch_size, device=device).bool()

				scores = [ scores[i] + score for i, score in enumerate(s) ]

			# append tokens
			for i, ri in enumerate(r):
				if stop_token in ri:
					stopped[i] = True
				sequence_list[i] = torch.cat([sequence_list[i], ri.to(device)])

			# stop token found
			stopped |= r == stop_token
			if stopped.all().item():
				break

		# pick the best scoring candidate
		# desu this is always going to be candidate 0
		if sampling_beam_width:
			sequence_list = [ sequence_list[0] ]

		sequence_list = [self._prune(r, stop_token) for r in sequence_list]
		return sequence_list


def example_usage():
	cfg.trainer.backend = "local"
	cfg.hyperparameters.gradient_accumulation_steps = 1
	if cfg.audio_backend == "dac":
		cfg.sample_rate = 44_100

	from functools import partial
	from einops import repeat
	from tqdm import tqdm

	from ..emb.qnt import decode_to_file, unload_model, trim_random, repeat_extend_audio, concat_audio, merge_audio
	from ..engines import Engine, Engines
	from ..utils import wrapper as ml
	
	import numpy as np
	import re

	device = "cuda"
	
	# mamba seems to ONLY be used as an AR (any NAR attempts lobotomizes it)
	"""
	if "mamba" in cfg.model.arch_type:
		cfg.model.resp_levels = 1
	"""
	# cfg.model.loss_factors = {}

	def tokenize(content):
		return torch.tensor( cfg.tokenizer.encode(content) )

	def _load_quants(path) -> Tensor:
		qnt = np.load(path, allow_pickle=True)[()]
		return torch.from_numpy(qnt["codes"].astype(np.int16))[0, :cfg.model.resp_levels, :].t().to(torch.int16)

	qnt = _load_quants(f"./data/qnt.{'dac' if cfg.audio_backend == 'dac' else 'enc'}")
	noise = _load_quants(f"./data/noise.{'dac' if cfg.audio_backend == 'dac' else 'enc'}")

	text_list = [
		tokenize("ˈaɪ wɪl nˌɑːt ˈæsk ɐ sˈɛkənd tˈaɪm").to(device),
		#tokenize("ˈaɪ wɪl nˌɑːt ˈæsk").to(device),
	]
	proms_list = [
		qnt[:cfg.dataset.frames_per_second, :].to(device),
		#qnt[:cfg.dataset.frames_per_second, :].to(device),
	]
	resps_list = [
		qnt[:, :].to(device),
		#qnt[:cfg.dataset.frames_per_second, :].to(device),
	]

	text_list = text_list[:1]
	proms_list = proms_list[:1]
	resps_list = resps_list[:1]

	batch_size = len(text_list)

	# rentet-full is the only configuration with BitNet's BitLinear that converges despite the grad_norm saying otherwise
	kwargs = {
		'n_text_tokens': 256,
		'n_audio_tokens': 1024,

		'd_model': 1024, # 256, # 1024, # 1536
		'n_heads': 16, # 4, # 16, # 24
		'n_layers': 12, # 32
		'n_experts': 1,

		'p_dropout': 0.1,

		'l_padding': 8 if cfg.optimizations.fp8 else 0,

		'config': cfg.model
	}
	
	"""
	try:
		kwargs['config'] = cfg.model
	except Exception as e:
		pass
	"""

	bos_id, space_id, eos_id = cfg.tokenizer.encode( " " )
	tasks = cfg.dataset.tasks_list

	model = AR_NAR(**kwargs).to(device)
	steps = 150 * len(tasks) * cfg.model.experimental.causal_size

	optimizer = cfg.hyperparameters.optimizer.lower() if cfg.yaml_path is not None else "prodigy"
	scheduler = cfg.hyperparameters.scheduler.lower() if cfg.yaml_path is not None else ""
	learning_rate = cfg.hyperparameters.learning_rate if cfg.yaml_path is not None else None

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
	else:
		raise ValueError(f"Unrecognized optimizer: {optimizer}")

	print("Optimizer:", optimizer, "\tLearning rate:", learning_rate)

	optimizer = optimizer(model.parameters(), lr=learning_rate)

	if scheduler == "schedulefree":
		if isinstance(optimizer, ml.AdamW):
			scheduler = ml.schedulefree.AdamWScheduleFree
		elif isinstance(optimizer, ml.SGD):
			scheduler = ml.schedulefree.SGDScheduleFree
		else:
			scheduler = None

		if scheduler is not None:
			print("Scheduler:", scheduler)
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

	print(f"AR+NAR ({cfg.model.arch_type}, {cfg.audio_backend}) parameter count: {sum(p.numel() for p in model.parameters() if p.requires_grad)}")

	@torch.no_grad()
	def sample_data(task=None):
		texts = []
		proms = []
		resps = []

		for i in range(batch_size):
			if task is None:
				task = random.choice(tasks)

			text = text_list[i]
			prom = proms_list[i]
			resp = resps_list[i]

			# do nothing
			if task == "tts":
				...
			elif task == "tts-c":
				trim_length = int(random.uniform(cfg.dataset.prompt_duration_range[0], cfg.dataset.prompt_duration_range[1]) * cfg.dataset.frames_per_second)

				prom = resp[:trim_length]
				resp = resp[trim_length:]
			elif task == "ns" or task == "sr":
				# extend the noise to fill the target audio
				noise_ext = repeat_extend_audio( noise, resp.shape[0] )
				# create the input prompt by merging the target audio with the noise
				prom = merge_audio( resp.cpu(), noise_ext, scale=[1, cfg.dataset.noise_scale], device=cfg.dataset.reencode_device )
				# set the target to just be the noise if <sr>
				if task == "sr":
					resp = noise_ext

				# set the text prompt to empty to train without a guided text prompt
				if random.random() < 0.5:
					text = torch.tensor([bos_id, eos_id]).to(device=device, dtype=torch.uint8)

			texts.append( text.to(device) )
			proms.append( prom.to(device) )
			resps.append( resp.to(device) )

		return texts, proms, resps

	@torch.inference_mode()
	def sample( name, steps=1000, task=None ):
		engine.eval()

		texts, proms, resps = sample_data( task )

		if "ar" in cfg.model.capabilities:
			resps = engine( texts, proms, max_steps=steps, sampling_temperature=0.95 )

		if "nar" in cfg.model.capabilities:
			resps = engine( texts, proms, resps, sampling_temperature=0.2 )

		for i, o in enumerate(resps):
			_ = decode_to_file(o.to(dtype=torch.int32), f"data/{cfg.model.arch_type}.{cfg.audio_backend}.{i}.{task}.{name}.wav", device=device)

		unload_model()

	def train():
		engine.train()
		t = trange(steps)
		for i in t:
			texts, proms, resps = sample_data()

			stats = {"step": i}
			stats |= engine.traverse(text_list=texts, proms_list=proms, resps_list=resps)
			stats |= {"grad_norm": engine.get_global_grad_norm()}

			tqdm.write(f"{stats}")

		"""
		torch.save( {
			'module': model.state_dict()
		}, f"./data/{cfg.model.arch_type}.pth" )
		"""

	#sample("init", 5)
	train()
	
	for task in tasks:
		sample("final", task=task)

	engines.quit()

if __name__ == "__main__":
	example_usage()