#!/usr/bin/env python3
"""Quick standalone test for ComfyUI connectivity and image generation.

Usage:
    uv run python scripts/test_comfyui_quick.py [--url http://172.20.114.213:8188]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path

import httpx

WORKFLOW_TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "app" / "services" / "comfyui" / "workflow_template.json"

TEST_POSITIVE_PROMPT = (
    "杭州西湖, 夏日清凉游, 水彩插画风格, "
    "color palette: #4A90D9, #7ED6A5, #FFF8E7, "
    "elements: 西湖断桥, 荷花, 远山. "
    "Professional travel poster, high quality, detailed illustration, "
    "reserve top and bottom copy space, no text, no watermark."
)
TEST_NEGATIVE_PROMPT = "文字, 水印, 低质量, text, logo, QR code, blurry, deformed"


async def test_connectivity(base_url: str) -> bool:
    print(f"\n{'='*60}")
    print(f"  ComfyUI 连接测试: {base_url}")
    print(f"{'='*60}")

    async with httpx.AsyncClient(timeout=10) as client:
        # 1. System stats
        print("\n[1/3] 检查 /system_stats ...")
        try:
            response = await client.get(f"{base_url}/system_stats")
            response.raise_for_status()
            stats = response.json()
            system_info = stats.get("system", {})
            print(f"  ✓ 操作系统: {system_info.get('os', 'unknown')}")
            print(f"  ✓ Python: {system_info.get('python_version', 'unknown')}")
            devices = stats.get("devices", [])
            for device in devices:
                print(f"  ✓ GPU: {device.get('name', 'unknown')} "
                      f"(VRAM: {device.get('vram_total', 0) // 1024 // 1024}MB)")
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ 连接失败: {exc}")
            return False

        # 2. Object info (available nodes)
        print("\n[2/3] 检查可用节点 /object_info ...")
        try:
            response = await client.get(f"{base_url}/object_info")
            response.raise_for_status()
            nodes = response.json()
            required_nodes = ["CheckpointLoaderSimple", "KSampler", "CLIPTextEncode",
                              "EmptyLatentImage", "VAEDecode", "SaveImage"]
            for node_name in required_nodes:
                status = "✓" if node_name in nodes else "✗"
                print(f"  {status} {node_name}")
                if node_name not in nodes:
                    print(f"    ⚠ 缺少必要节点: {node_name}")
                    return False
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ 获取节点信息失败: {exc}")
            return False

        # 3. Check available checkpoints
        print("\n[3/3] 检查可用模型 ...")
        try:
            ckpt_info = nodes.get("CheckpointLoaderSimple", {})
            ckpts = ckpt_info.get("input", {}).get("required", {}).get("ckpt_name", [[]])[0]
            if ckpts:
                for ckpt in ckpts[:10]:
                    print(f"  ✓ {ckpt}")
            else:
                print("  ⚠ 未找到 checkpoint 模型文件")
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠ 无法列出模型: {exc}")

    return True


async def test_generation(base_url: str) -> bool:
    print(f"\n{'='*60}")
    print("  文生图生成测试")
    print(f"{'='*60}")

    workflow = json.loads(WORKFLOW_TEMPLATE_PATH.read_text(encoding="utf-8"))
    workflow["6"]["inputs"]["text"] = TEST_POSITIVE_PROMPT
    workflow["7"]["inputs"]["text"] = TEST_NEGATIVE_PROMPT
    workflow["3"]["inputs"]["seed"] = random.randint(0, 2**53)

    print(f"\n  模型: {workflow['4']['inputs']['ckpt_name']}")
    print(f"  尺寸: {workflow['5']['inputs']['width']}x{workflow['5']['inputs']['height']}")
    print(f"  步数: {workflow['3']['inputs']['steps']}, CFG: {workflow['3']['inputs']['cfg']}")
    print(f"  采样器: {workflow['3']['inputs']['sampler_name']} / {workflow['3']['inputs']['scheduler']}")

    async with httpx.AsyncClient(timeout=30) as client:
        # Submit prompt
        print("\n  提交生成任务 ...")
        start_time = time.time()
        try:
            response = await client.post(f"{base_url}/prompt", json={"prompt": workflow})
            response.raise_for_status()
            prompt_id = response.json()["prompt_id"]
            print(f"  ✓ 任务已提交, prompt_id: {prompt_id}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ 提交失败: {exc}")
            return False

        # Poll for completion
        print("  等待生成完成 ...")
        timeout = 300.0
        elapsed = 0.0
        while elapsed < timeout:
            await asyncio.sleep(2)
            elapsed = time.time() - start_time
            try:
                history_response = await client.get(f"{base_url}/history/{prompt_id}")
                history_response.raise_for_status()
                history = history_response.json()
                if prompt_id in history:
                    outputs = history[prompt_id].get("outputs", {})
                    for node_output in outputs.values():
                        images = node_output.get("images", [])
                        if images:
                            image = images[0]
                            filename = image["filename"]
                            subfolder = image.get("subfolder", "")
                            image_type = image.get("type", "output")
                            view_url = (
                                f"{base_url}/view?filename={filename}"
                                f"&subfolder={subfolder}&type={image_type}"
                            )
                            gen_time = time.time() - start_time
                            print(f"\n  ✓ 生成完成! 耗时: {gen_time:.1f}s")
                            print(f"  ✓ 文件名: {filename}")
                            print(f"  ✓ 预览URL: {view_url}")

                            # Verify image is downloadable
                            img_resp = await client.get(view_url)
                            img_resp.raise_for_status()
                            size_kb = len(img_resp.content) / 1024
                            print(f"  ✓ 图片大小: {size_kb:.0f} KB")
                            return True
                    print("  ✗ 任务完成但未找到图片输出")
                    return False
            except httpx.HTTPError:
                pass

            print(f"    ... 已等待 {elapsed:.0f}s", end="\r")

        print(f"\n  ✗ 超时 ({timeout}s)")
        return False


async def main() -> int:
    parser = argparse.ArgumentParser(description="ComfyUI 连接与生成测试")
    parser.add_argument("--url", default="http://10.29.248.167:8188", help="ComfyUI API URL")
    parser.add_argument("--skip-generation", action="store_true", help="仅测试连接，不生成图片")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")

    connected = await test_connectivity(base_url)
    if not connected:
        print("\n❌ 连接测试失败，请检查 ComfyUI 服务是否启动")
        return 1

    print("\n✅ 连接测试通过!")

    if args.skip_generation:
        return 0

    generated = await test_generation(base_url)
    if not generated:
        print("\n❌ 生成测试失败")
        return 1

    print("\n✅ 全部测试通过! ComfyUI 文生图服务配置正确。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
