import argparse

import torch
import torch.nn

from .data import get_phone_symmap
from .engines import load_engines
from .config import cfg

# stitches embeddings into one embedding + classifier => lm_head
def convert_to_hf( state_dict, config = None ):
	n_tokens = 256 + (1024 * 8) + (1024 * 8) + 1
	token_dim = 1024
	embedding = torch.nn.Embedding(n_tokens, token_dim)
	embedding.weight.requires_grad = False

	def move_value(k):
		v = state_dict['module'][k]
		del state_dict['module'][k]
		return v

	separator = move_value('sep')
	out_proj = move_value('classifier.weight')
	text_emb = move_value('text_emb.weight')
	langs_emb = move_value('langs_emb.weight')
	tasks_emb = move_value('tasks_emb.weight')
	tones_emb = move_value('tones_emb.weight')
	
	proms_emb_weight = [ move_value(f'proms_emb.weight.{i}').item() for i in range(8) ] if "proms_emb.weight.0" in state_dict['module'] else [ [ 1 for _ in range(8) ] ]
	resps_emb_weight = [ move_value(f'resps_emb.weight.{i}').item() for i in range(8) ] if "resps_emb.weight.0" in state_dict['module'] else [ [ 1 for _ in range(8) ] ]

	proms_emb = [ move_value(f'proms_emb.embeddings.{i}.weight') for i in range(8) ]
	resps_emb = [ move_value(f'resps_emb.embeddings.{i}.weight') for i in range(8) ]


	start = 0
	for i in range(256):
		embedding.weight[start + i] = text_emb[i]
	
	start = 256
	for layer in range(8):
		for i in range(1024):
			offset = start + 1024 * layer
			embedding.weight[i + offset] = proms_emb[layer][i] * proms_emb_weight[layer]
			
	start = 256 + 1024 * 8
	for layer in range(8):
		for i in range(1024):
			offset = start + 1024 * layer
			embedding.weight[i + offset] = resps_emb[layer][i] * proms_emb_weight[layer]

	state_dict['module']['model.embed_tokens.weight'] = embedding.state_dict()
	state_dict['module']['lm_head.weight'] = out_proj
	del state_dict['module']['classifier.bias']

	torch.save(state_dict, "./data/export_test.pth")

	raise Exception("!")

	return state_dict

def main():
	parser = argparse.ArgumentParser("Save trained model to path.")
	parser.add_argument("--module-only", action='store_true')
	parser.add_argument("--hf", action='store_true', default=None) # convert to HF-style
	args = parser.parse_args()

	if args.module_only:
		cfg.trainer.load_module_only = True

	callback = convert_to_hf if args.hf else None

	engines = load_engines()
	engines.export(userdata={"symmap": get_phone_symmap()}, callback=callback)

if __name__ == "__main__":
	main()