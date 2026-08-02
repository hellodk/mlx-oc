import argparse
import time

import mlx.core as mx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="ring")
    args = parser.parse_args()

    t0 = time.time()
    world = mx.distributed.init(strict=True, backend=args.backend)
    rank = world.rank()
    size = world.size()

    x = mx.distributed.all_sum(mx.ones(8) * (rank + 1))
    expected = sum(range(1, size + 1))
    ok = all(v == expected for v in x.tolist())

    print(
        f"[rank {rank}] size={size} all_sum={x.tolist()} "
        f"expected={expected} ok={ok} init={time.time()-t0:.2f}s",
        flush=True,
    )


if __name__ == "__main__":
    main()
