import mlx.core as mx
from mlx_lm.utils import sharded_load
group = mx.distributed.init()
print(f'[rank {group.rank()}] sharded_load start', flush=True)
model, _ = sharded_load('mlx-community/Qwen3.5-4B-MLX-4bit', tensor_group=group, tokenizer_config={})
mx.eval(model)
print(f'[rank {group.rank()}] sharded_load OK', flush=True)
print(f'[rank {group.rank()}] DONE', flush=True)
