import argparse
import random
import string
import torch

from functools import cache
from pathlib import Path
from phonemizer import phonemize
from phonemizer.backend import BACKENDS

from tqdm import tqdm

try:
	import pykakasi
except Exception as e:
	pass

@cache
def _get_graphs(path):
	with open(path, "r") as f:
		graphs = f.read()
	return graphs

def romanize( runes, sep="" ):	
	kks = pykakasi.kakasi()
	result = kks.convert( runes )
	return sep.join([ res['hira'] for res in result ])

cached_backends = {}
def _get_backend( language="en-us", backend="espeak" ):
	key = f'{language}_{backend}'
	if key in cached_backends:
		return cached_backends[key]

	if backend == 'espeak':
		phonemizer = BACKENDS[backend]( language, preserve_punctuation=True, with_stress=True)
	elif backend == 'espeak-mbrola':
		phonemizer = BACKENDS[backend]( language )
	else: 
		phonemizer = BACKENDS[backend]( language, preserve_punctuation=True )

	cached_backends[key] = phonemizer
	return phonemizer


def encode(text: str, language="en-us", backend="auto") -> list[str]:
	if language == "en":
		language = "en-us"

	# Convert to kana because espeak does not like kanji...
	if language[:2] == "ja" and backend == "auto":
		text = romanize( text )

	if not backend or backend == "auto":
		backend = "espeak" # if language[:2] != "en" else "festival"

	text = [ text ]

	backend = _get_backend(language=language, backend=backend)
	if backend is not None:
		tokens = backend.phonemize( text, strip=True )
	else:
		tokens = phonemize( text, language=language, strip=True, preserve_punctuation=True, with_stress=True )
	

	tokens = list(tokens[0])

	return tokens