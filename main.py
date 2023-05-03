import argparse

import torch

import transformers
from transformers import AutoConfig, AutoTokenizer, AutoModelForCausalLM

import datasets
import wandb

from tqdm import tqdm
from loguru import logger

from peft_pretraining.relora import ReLoRaModel


def parse_args(args):
    parser = argparse.ArgumentParser()

    parser.add_argument("--model_config", type=str, required=True)

    parser.add_argument("--batch_size", type=int, required=True)
    parser.add_argument("--max_length", type=int, default=512)

    parser.add_argument("--use_peft", action="store_true")
    parser.add_argument("--lora_r", type=int, default=128)
    parser.add_argument("--relora", type=int, default=None)

    parser.add_argument("--train_ln", action="store_true")
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--gradient_accumulation", type=int, default=1)
    parser.add_argument("--warmup_steps", type=int, default=1_000)

    parser.add_argument("--num_training_steps", type=int, default=10_000)
    parser.add_argument("--device", type=str, default=None)
    parser.add_argument("--dtype", type=str, default="bfloat16")

    args = parser.parse_args(args)
    return args


def main(args):
    logger.info("*" * 40)
    logger.info(f"Starting training with the arguments")
    # use f-string formatting to align the arguments
    for k, v in vars(args).items():
        logger.info(f"{k:20} {v}")
    logger.info("*" * 40)

    dataset_name = "c4"  # switch to "togethercomputer/RedPajama-Data-1T" later
    if dataset_name == "c4":
        data = datasets.load_dataset("c4", "en", split="train", streaming=True)
    else:
        data = datasets.load_dataset(dataset_name, split="train", streaming=True)

    data = data.shuffle(seed=42)

    # it doesn't matter which tokenizer we use, because we train from scratch
    # T5 tokenizer was trained on C4 and we are also training on C4, so it's a good choice
    tokenizer = AutoTokenizer.from_pretrained("t5-base")

    def preprocess_batched(batch):
        batch = tokenizer(
            batch["text"],
            max_length=args.max_length,
            truncation=True,
            padding="max_length",
            return_tensors="pt",
        )
        return batch

    data_mapped = data.map(
        preprocess_batched,
        batched=True,
        remove_columns=["text", "timestamp", "url"],
    )

    def collate_fn(batch_list):
        batch = {
            "input_ids": torch.stack([example["input_ids"] for example in batch_list]),
            "attention_mask": torch.stack([example["attention_mask"] for example in batch_list]),
        }
        return batch

    def batch_fn(dataset, batch_size):
        batch = []
        for example in dataset:
            batch.append(example)
            if len(batch) == batch_size:
                batch = collate_fn(batch)
                yield batch
                batch = []
        if len(batch) > 0:
            yield batch

    data_mapped.batch = lambda batch_size: batch_fn(data_mapped, batch_size)

    device = args.device or "cuda"

    model_config = AutoConfig.from_pretrained(args.model_config)

    model = AutoModelForCausalLM.from_config(model_config)

    params_before = sum(p.numel() for p in model.parameters())
    trainable_before = sum(p.numel() for p in model.parameters() if p.requires_grad)

    if args.use_peft:
        for p in model.parameters():
            p.requires_grad = False

        model = ReLoRaModel(
            model,
            r=args.lora_r,
            lora_alpha=32,
            lora_dropout=0.1,
            target_modules=["attn", "mlp"],
        )

        for name, param in model.named_parameters():
            # LLaMa
            # model.norm, model.layers.input_layernorm, model.layers.post_attention_layernorm
            if args.train_ln and "norm" in name:
                param.requires_grad = True        
            elif "lm_head" in name:
                param.requires_grad = True
            elif "embed_tokens" in name:
                param.requires_grad = True
            elif "bias" in name:
                param.requires_grad = True
            elif "lora_" in name:
                param.requires_grad = True
            else:
                param.requires_grad = False

    params_after = sum(p.numel() for p in model.parameters())
    trainable_after = sum(p.numel() for p in model.parameters() if p.requires_grad)

    # print params and trainable params
    print(model)
    logger.info(f"Total params before LoRA: {params_before / 1_000_000:.2f}M")
    logger.info(f"Total params after  LoRA: {params_after / 1_000_000:.2f}M")
    logger.info(f"Trainable params: {sum(p.numel() for p in model.parameters() if p.requires_grad) / 1_000_000:.2f}M")

    if args.use_peft:
        if (params_after <= params_before):
            raise ValueError("Total number of parameters should increase after applying LoRA")
        
        if (trainable_after >= trainable_before):
            raise ValueError("Total number of trainable parameters should decrease after applying LoRA")

    model = model.to(device, dtype=getattr(torch, args.dtype))

    n_total_params = sum(p.numel() for p in model.parameters())
    n_trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    p_trainable_params = n_trainable_params / n_total_params

    trainable_params = (p for p in model.parameters() if p.requires_grad)
    trainable_params_names = [name for name, p in model.named_parameters() if p.requires_grad]

    optimizer = torch.optim.Adam(trainable_params, lr=args.lr)
    scheduler = transformers.get_linear_schedule_with_warmup(
        optimizer, num_warmup_steps=args.warmup_steps, num_training_steps=args.num_training_steps,
    )

    # from args
    _config = vars(args)
    _config["max_lr"] = _config.pop("lr")  # rename lr to max_lr
    _config_ext = {
        "total_params": n_total_params,
        "total_params_M": n_total_params / 1_000_000,
        "trainable_params": n_trainable_params,
        "trainable_params_M": n_trainable_params / 1_000_000,
        "percent_trainable_params": p_trainable_params,
        "name_trainable_params": trainable_params_names,
        "dataset": dataset_name,
        "model": model_config.to_dict(),
        "scheduler": "linear",
        "device": str(device),
    }
    _config.update(_config_ext)

    if args.use_peft:
        logger.warning("PEFT config (all but lora_r) is hardcoded!")
        _config["peft_config"] = {
            "r": args.lora_r,
            "alpha": 32,
            "dropout": 0.1,
            "target_modules": ["attn", "mlp"],
        }

    wandb.init(project="peft_pretraining", config=_config)
    pbar = tqdm(total=args.num_training_steps)

    global_step = 0
    update_step = 0
    for epoch in range(1):  # we'll probably never go through all the data
        data_mapped.set_epoch(epoch)
        for batch in data_mapped.batch(batch_size=args.batch_size):
            global_step += 1
            pbar.update(1)
            if global_step > args.num_training_steps * args.gradient_accumulation:
                logger.info(f"Reached max number of update steps (f{args.num_training_steps}). Stopping training.")
                break

            optimizer.zero_grad()

            batch = {k: v.to(device) for k, v in batch.items()}
            labels = batch["input_ids"].clone()
            labels[labels == 0] = -100

            loss = model(**batch, labels=labels).loss / args.gradient_accumulation
            loss.backward()

            if global_step % args.gradient_accumulation != 0:
                continue

            update_step += 1
            optimizer.step()
            scheduler.step()

            if args.relora and update_step % args.relora == 0:
                print("In merge and reinit")
                model.merge_and_reinit()

            lr = scheduler.get_last_lr()[0]
            tokens_seen = global_step * args.max_length * args.batch_size

            wandb.log({
                "loss": loss.item(),
                "lr": lr,
                "update_step": update_step,
                "tokens_seen": tokens_seen,
                },
                step=global_step,
            )

    pbar.close()
    logger.info("Training finished")


if __name__ == "__main__":
    args = parse_args(None)
    main(args)