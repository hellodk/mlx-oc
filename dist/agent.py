import argparse
import ast
import json
import operator
import re
import time

import mlx.core as mx
from mlx_lm.generate import generate
from mlx_lm.utils import sharded_load

DEFAULT_MODEL = "mlx-community/Qwen3-1.7B-4bit"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a given city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "Name of the city, e.g. Tokyo",
                    }
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Evaluate an arithmetic expression and return the numeric result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Arithmetic expression, e.g. 23*17",
                    }
                },
                "required": ["expression"],
            },
        },
    },
]

SYSTEM = "You are a helpful assistant that uses the provided tools to answer questions accurately."

USER_MSG = (
    "What is the weather in Tokyo right now, and what is 23 times 17? "
    "Use the tools to find out."
)


def safe_calc(expr: str):
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(
            node,
            (
                ast.Expression,
                ast.BinOp,
                ast.UnaryOp,
                ast.Constant,
                ast.Add,
                ast.Sub,
                ast.Mult,
                ast.Div,
                ast.FloorDiv,
                ast.Mod,
                ast.Pow,
                ast.USub,
                ast.UAdd,
            ),
        ):
            raise ValueError("unsupported expression")
    return eval(compile(tree, "<expr>", "eval"), {"__builtins__": {}}, {})


def execute_tool(name: str, arguments: dict):
    if name == "get_weather":
        city = arguments.get("city", "Unknown")
        return {
            "city": city,
            "temperature_c": 24,
            "condition": "partly cloudy",
            "humidity_percent": 62,
        }
    if name == "calculate":
        expr = arguments.get("expression", "")
        try:
            result = safe_calc(expr)
        except Exception as e:
            return {"expression": expr, "error": str(e)}
        return {"expression": expr, "result": result}
    return {"error": f"unknown tool: {name}"}


def parse_tool_calls(text: str):
    calls = []
    for block in re.findall(r"<tool_call>(.*?)</tool_call>", text, re.DOTALL):
        try:
            data = json.loads(block.strip())
            calls.append(data)
        except Exception as e:
            print(f"  [warn] failed to parse tool_call block: {e}", flush=True)
    return calls


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--backend", default="ring")
    parser.add_argument("--local", action="store_true", help="run on a single node")
    args = parser.parse_args()

    if args.local:
        world = mx.distributed.init(strict=False)
        rank = world.rank()
        size = world.size()
        print(f"[rank {rank}] single-node mode (size={size})", flush=True)
        t0 = time.time()
        from mlx_lm import load

        model, tokenizer = load(args.model)
        print(f"[rank {rank}] model loaded in {time.time()-t0:.1f}s", flush=True)
    else:
        world = mx.distributed.init(strict=True, backend=args.backend)
        rank = world.rank()
        size = world.size()
        print(f"[rank {rank}] world size={size} backend initialized", flush=True)
        t0 = time.time()
        model, tokenizer = sharded_load(args.model, tensor_group=world)
        print(f"[rank {rank}] model loaded in {time.time()-t0:.1f}s", flush=True)

    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": USER_MSG},
    ]

    final_response = None
    for step in range(4):
        prompt = tokenizer.apply_chat_template(
            messages,
            tools=TOOLS,
            tokenize=True,
            add_generation_prompt=True,
            enable_thinking=False,
        )

        g0 = time.time()
        response = generate(
            model,
            tokenizer,
            prompt=prompt,
            max_tokens=args.max_tokens,
        )
        gen_s = time.time() - g0
        final_response = response

        if rank == 0:
            print(f"\n[rank {rank}] --- assistant turn {step} ({gen_s:.1f}s) ---", flush=True)
            print(response, flush=True)

        calls = parse_tool_calls(response)
        if not calls:
            break

        messages.append({"role": "assistant", "content": response})
        for call in calls:
            name = call.get("name", "?")
            arguments = call.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except Exception:
                    arguments = {}
            result = execute_tool(name, arguments)
            messages.append({"role": "tool", "name": name, "content": json.dumps(result)})
            if rank == 0:
                print(
                    f"[rank {rank}] tool {name}({json.dumps(arguments)}) -> {json.dumps(result)}",
                    flush=True,
                )

    if rank == 0:
        print("\n[rank 0] final answer:", final_response, flush=True)


if __name__ == "__main__":
    main()
