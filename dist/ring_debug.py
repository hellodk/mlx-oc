import os
print("RANK", os.environ.get("MLX_RANK"))
hf = os.environ.get("MLX_HOSTFILE")
if hf:
    print("HOSTFILE", open(hf).read().strip())
import mlx.core as mx
g = mx.distributed.init()
print("init ok, rank", g.rank(), "size", g.size())
