import mlx.core as mx
d = mx.distributed
group = d.init()
print(f'[rank {group.rank()}] init size={group.size()}', flush=True)
x = mx.ones((2, 2)) * (group.rank() + 1)
y = d.all_sum(x)
mx.eval(y)
print(f'[rank {group.rank()}] all_sum OK {y.tolist()}', flush=True)
print(f'[rank {group.rank()}] DONE', flush=True)
