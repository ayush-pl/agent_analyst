"""Turn a QLoRA fine-tune into a quantized GGUF the demo can run on CPU, and upload it.

Run this on the machine that has your adapter (your 8 GB GPU box is fine; merging
also works on CPU with ~16 GB RAM for a 3B model).

    pip install torch transformers peft huggingface_hub
    git clone --depth 1 https://github.com/ggml-org/llama.cpp
    pip install -r llama.cpp/requirements.txt
    cmake -S llama.cpp -B llama.cpp/build && cmake --build llama.cpp/build --target llama-quantize -j

    python deploy/export_gguf.py \\
        --base Qwen/Qwen2.5-Coder-3B-Instruct \\
        --adapter path/to/your/qlora-adapter \\
        --llama-cpp llama.cpp \\
        --upload <hf-user>/qwen2.5-coder-3b-text2sql-GGUF

Then point the agent at it:
    ANALYST_SQL_MODEL=hf://<hf-user>/qwen2.5-coder-3b-text2sql-GGUF/model-q4_k_m.gguf

Steps: (1) load the base model in fp16 (not 4-bit: merging into quantized weights
loses accuracy), (2) merge the LoRA weights, (3) convert to GGUF f16 with llama.cpp,
(4) quantize to Q4_K_M (~2 GB, the size/quality sweet spot for CPU), (5) upload.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="Qwen/Qwen2.5-Coder-3B-Instruct", help="base model your adapter was trained on")
    ap.add_argument("--adapter", required=True, help="PEFT adapter dir (adapter_config.json + weights) or HF repo")
    ap.add_argument("--llama-cpp", required=True, type=Path, help="path to a llama.cpp checkout (built)")
    ap.add_argument("--out", default="export", type=Path)
    ap.add_argument("--quant", default="Q4_K_M")
    ap.add_argument("--upload", help="HF model repo to upload the GGUF to, e.g. <hf-user>/my-model-GGUF")
    args = ap.parse_args()

    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    merged = args.out / "merged"
    print("1/4 merging adapter into fp16 base ...", flush=True)
    model = AutoModelForCausalLM.from_pretrained(args.base, torch_dtype=torch.float16, device_map="cpu")
    model = PeftModel.from_pretrained(model, args.adapter).merge_and_unload()
    model.save_pretrained(merged, safe_serialization=True)
    AutoTokenizer.from_pretrained(args.adapter if Path(args.adapter, "tokenizer_config.json").exists()
                                  else args.base).save_pretrained(merged)

    f16 = args.out / "model-f16.gguf"
    print("2/4 converting to GGUF ...", flush=True)
    subprocess.run([sys.executable, str(args.llama_cpp / "convert_hf_to_gguf.py"), str(merged),
                    "--outfile", str(f16), "--outtype", "f16"], check=True)

    quant = args.out / f"model-{args.quant.lower()}.gguf"
    print(f"3/4 quantizing to {args.quant} ...", flush=True)
    exe = next((p for p in [args.llama_cpp / "build/bin/llama-quantize", args.llama_cpp / "llama-quantize"]
                if p.exists()), None)
    if exe is None:
        sys.exit("llama-quantize not found: build it with cmake (see the docstring).")
    subprocess.run([str(exe), str(f16), str(quant), args.quant], check=True)

    if args.upload:
        from huggingface_hub import HfApi

        print("4/4 uploading ...", flush=True)
        api = HfApi()
        api.create_repo(args.upload, repo_type="model", exist_ok=True)
        api.upload_file(path_or_fileobj=str(quant), path_in_repo=quant.name, repo_id=args.upload)
        print(f"ANALYST_SQL_MODEL=hf://{args.upload}/{quant.name}")
    else:
        print(f"ANALYST_SQL_MODEL={quant.resolve()}")


if __name__ == "__main__":
    main()
