import torch
import torchaudio
import soundfile
import time

from torch import Tensor
from einops import rearrange
from pathlib import Path

from .emb import g2p, qnt
from .emb.qnt import trim, trim_random, unload_model
from .utils import to_device, set_seed, wrapper as ml

from .config import cfg, Config
from .models import get_models
from .engines import load_engines, deepspeed_available
from .data import get_phone_symmap, get_lang_symmap, _load_quants, _cleanup_phones, tokenize

if deepspeed_available:
	import deepspeed

class TTS():
	def __init__( self, config=None, device=None, amp=None, dtype=None ):
		self.loading = True 

		self.load_config( config=config, device=device, amp=amp, dtype=dtype )	
		self.load_model()

		self.loading = False 

	def load_config( self, config=None, device=None, amp=None, dtype=None ):
		if config:
			print("Loading YAML:", config)
			cfg.load_yaml( config )

		try:
			cfg.format( training=False )
			cfg.dataset.use_hdf5 = False # could use cfg.load_hdf5(), but why would it ever need to be loaded for inferencing
		except Exception as e:
			print("Error while parsing config YAML:")
			raise e # throw an error because I'm tired of silent errors messing things up for me

		if amp is None:
			amp = cfg.inference.amp
		if dtype is None or dtype == "auto":
			dtype = cfg.inference.weight_dtype
		if device is None:
			device = cfg.device

		cfg.device = device
		cfg.mode = "inferencing"
		cfg.trainer.backend = cfg.inference.backend
		cfg.trainer.weight_dtype = dtype
		cfg.inference.weight_dtype = dtype

		self.device = device
		self.dtype = cfg.inference.dtype
		self.amp = amp
		

	def load_model( self ):
		load_engines.cache_clear()
		unload_model()
		
		self.engines = load_engines(training=False)
		for name, engine in self.engines.items():
			if self.dtype != torch.int8:
				engine.to(self.device, dtype=self.dtype if not self.amp else torch.float32)

		self.engines.eval()
		self.symmap = get_phone_symmap()
		print("Loaded model")

	def encode_text( self, text, language="en" ):
		# already a tensor, return it
		if isinstance( text, Tensor ):
			return text

		content = g2p.encode(text, language=language)
		tokens = tokenize( content )

		return torch.tensor( tokens )

	def encode_lang( self, language ):
		symmap = get_lang_symmap()
		id = 0
		if language in symmap:
			id = symmap[language]
		return torch.tensor([ id ])

	def encode_audio( self, paths, trim_length=0.0 ):
		# already a tensor, return it
		if isinstance( paths, Tensor ):
			return paths

		# split string into paths
		if isinstance( paths, str ):
			paths = [ Path(p) for p in paths.split(";") ]

		# merge inputs

		proms = []

		for path in paths:
			prom = qnt.encode_from_file(path)
			if hasattr( prom, "codes" ):
				prom = prom.codes
			prom = prom[0][:, :].t().to(torch.int16)

			proms.append( prom )

		res = torch.cat(proms)
		
		if trim_length:
			res = trim( res, int( cfg.dataset.frames_per_second * trim_length ) )
		
		return res

	@torch.inference_mode()
	def inference(
		self,
		text,
		references,
		language="en",
		max_ar_steps=6 * cfg.dataset.frames_per_second,
		max_nar_levels=7,
		input_prompt_length=0.0,
		ar_temp=0.95,
		nar_temp=0.5,
		min_ar_temp=0.95,
		min_nar_temp=0.5,
		top_p=1.0,
		top_k=0,
		repetition_penalty=1.0,
		repetition_penalty_decay=0.0,
		length_penalty=0.0,
		beam_width=0,
		mirostat_tau=0,
		mirostat_eta=0.1,

		seed = None,

		out_path=None,

		tqdm=True,
	):
		lines = text.split("\n")

		wavs = []
		sr = None

		model_ar = None
		model_len = None
		model_nar = None

		for name, engine in self.engines.items():
			if "ar" in engine.hyper_config.capabilities:
				model_ar = engine.module
			if "len" in engine.hyper_config.capabilities:
				model_len = engine.module
			if "nar" in engine.hyper_config.capabilities:
				model_nar = engine.module
		
		set_seed(seed)

		for line in lines:
			if out_path is None:
				output_dir = Path("./data/results/")
				if not output_dir.exists():
					output_dir.mkdir(parents=True, exist_ok=True)
				out_path = output_dir / f"{time.time()}.wav"

			prom = self.encode_audio( references, trim_length=input_prompt_length )
			phns = self.encode_text( line, language=language )
			lang = self.encode_lang( language )

			prom = to_device(prom, self.device).to(torch.int16)
			phns = to_device(phns, self.device).to(torch.uint8 if len(self.symmap) < 256 else torch.int16)
			lang = to_device(lang, self.device).to(torch.uint8)

			text_list = [ phns ]
			proms_list = [ prom ]

			with torch.autocast("cuda", dtype=self.dtype, enabled=self.amp):
				# AR temp: 1
				# NAR temp: 0.05
				# prom size: 3
				if model_ar is not None:
					resps_list = model_ar(
						text_list=[phns], proms_list=[prom], lang_list=[lang], max_steps=max_ar_steps,
						sampling_temperature=ar_temp,
						sampling_min_temperature=min_ar_temp,
						sampling_top_p=top_p, sampling_top_k=top_k,
						sampling_repetition_penalty=repetition_penalty, sampling_repetition_penalty_decay=repetition_penalty_decay,
						sampling_length_penalty=length_penalty,
						sampling_beam_width=beam_width,
						sampling_mirostat_tau=mirostat_tau,
						sampling_mirostat_eta=mirostat_eta,

						disable_tqdm=not tqdm,
					)
					resps_list = model_nar(
						text_list=[phns], proms_list=[prom], lang_list=[lang], resps_list=resps_list,
						max_levels=max_nar_levels,
						sampling_temperature=nar_temp,
						sampling_min_temperature=min_nar_temp,
						sampling_top_p=top_p, sampling_top_k=top_k,
						sampling_repetition_penalty=repetition_penalty, sampling_repetition_penalty_decay=repetition_penalty_decay,

						disable_tqdm=not tqdm,
					)
				elif model_len is not None:
					len_list = model_len( text_list=[phns], proms_list=[prom], max_steps=10, disable_tqdm=not tqdm ) # don't need more than that
					resps_list = model_nar( text_list=[phns], proms_list=[prom], len_list=len_list,
						max_levels=max_nar_levels,
						sampling_temperature=nar_temp,
						sampling_min_temperature=min_nar_temp,
						sampling_top_p=top_p, sampling_top_k=top_k,
						sampling_repetition_penalty=repetition_penalty, sampling_repetition_penalty_decay=repetition_penalty_decay,

						disable_tqdm=not tqdm,
					)
				else:
					raise Exception("!")

			wav, sr = qnt.decode_to_file(resps_list[0], out_path, device=self.device)
			wavs.append(wav)
		
		return (torch.concat(wavs, dim=-1), sr)
		
