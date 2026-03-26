#!/usr/bin/env python3
"""
通过 MapScript.galaxy 恢复一个可用的 Triggers XML。

说明：
- 该脚本优先覆盖常见近战初始化触发器（与样例 1~5 对应）。
- 对未知函数调用会跳过并给出 warning（因为缺少 FunctionDef 映射时无法可靠写入 Triggers）。
"""

from __future__ import annotations

import argparse
import hashlib
import textwrap
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


# MapScript 中的函数名 -> Triggers 里的 Ntve FunctionDef Id
NTVE_FUNCTION_MAP = {
    "TriggerAddEventMapInit": "00000120",  # Event: Map Initialization
    "MeleeInitResources": "00000143",
    "MeleeInitUnits": "00000145",
    "MeleeInitOptions": "00000148",
    "MeleeInitAI": "00000150",
}

NTVE_CUSTOM_SCRIPT_FUNCTION_DEF = "00000123"


def stable_id(key: str) -> str:
    """生成 8 位大写十六进制 ID（稳定、可复现）。"""
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:8].upper()


def extract_block(text: str, func_name: str) -> str | None:
    """提取函数体（包含最外层大括号内内容）。"""
    m = re.search(rf"\b{re.escape(func_name)}\s*\([^)]*\)\s*\{{", text)
    if not m:
        return None
    i = m.end() - 1  # at '{'
    depth = 0
    start = i + 1
    for j in range(i, len(text)):
        ch = text[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:j]
    return None


def extract_run_actions_block(func_body: str) -> str:
    """尽量提取 `if (!runActions) return` 之后的动作区域。"""
    m = re.search(r"if\s*\(\s*!runActions\s*\)\s*\{", func_body)
    if not m:
        return func_body

    i = m.end() - 1
    depth = 0
    guard_end = None
    for j in range(i, len(func_body)):
        ch = func_body[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                guard_end = j + 1
                break

    if guard_end is None:
        return func_body

    tail = func_body[guard_end:]
    # 去掉末尾 return true;（常见模板）
    tail = re.sub(r"\s*return\s+true\s*;\s*$", "", tail, flags=re.S)
    return tail


def find_custom_script_includes(text: str) -> list[str]:
    """提取 include "scripts/..." 这类可回填到 Triggers 的 CustomScript。"""
    includes = re.findall(r'^\s*include\s+"(scripts/[^"]+)"\s*$', text, flags=re.M)
    return [f'include "{inc}"' for inc in includes]


def parse_triggers_from_mapscript(text: str) -> list[dict]:
    """解析 MapScript.galaxy，提取触发器、事件、动作。"""
    triggers: list[dict] = []

    # 触发器变量定义顺序即 Root 顺序
    trigger_vars = re.findall(r"^\s*trigger\s+([A-Za-z_]\w*)\s*;", text, flags=re.M)

    for trig_var in trigger_vars:
        trig = {
            "name": trig_var,
            "events": [],
            "actions": [],
            "script_action": None,
        }

        # 1) 从 Init 函数提取事件
        init_body = extract_block(text, f"{trig_var}_Init")
        if init_body and re.search(rf"\bTriggerAddEventMapInit\s*\(\s*{re.escape(trig_var)}\s*\)\s*;", init_body):
            trig["events"].append("TriggerAddEventMapInit")

        # 2) 从 Func 函数提取动作（只保留映射内函数）
        func_body = extract_block(text, f"{trig_var}_Func")
        if func_body:
            actions_block = extract_run_actions_block(func_body)

            mapped_lines: set[str] = set()
            for full_call, call in re.findall(r"(^\s*([A-Za-z_]\w*)\s*\([^;]*\)\s*;)", actions_block, flags=re.M):
                if call in {"return", "if"}:
                    continue
                if call in NTVE_FUNCTION_MAP and call != "TriggerAddEventMapInit":
                    trig["actions"].append(call)
                    mapped_lines.add(full_call.strip())

            # 未映射语句尽量合并为 Custom Script Action
            script_lines: list[str] = []
            for line in actions_block.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped in {"// Actions", "return true;", "{" , "}"}:
                    continue
                if stripped.startswith("//"):
                    continue
                if stripped in mapped_lines:
                    continue
                script_lines.append(line.rstrip())

            if script_lines:
                trig["script_action"] = textwrap.dedent("\n".join(script_lines)).strip()

        triggers.append(trig)

    return triggers


def build_trigger_xml(triggers: list[dict], custom_scripts: list[str] | None = None) -> ET.ElementTree:
    root = ET.Element("TriggerData")

    root_node = ET.SubElement(root, "Root")

    # Custom Script 根节点（如 include "scripts/TheThing"）
    custom_scripts = custom_scripts or []
    for script_line in custom_scripts:
        cs_id = stable_id(f"customscript:{script_line}")
        ET.SubElement(root_node, "Item", {"Type": "CustomScript", "Id": cs_id})

    for trig in triggers:
        trig_id = stable_id(f"trigger:{trig['name']}")
        ET.SubElement(root_node, "Item", {"Type": "Trigger", "Id": trig_id})

    for script_line in custom_scripts:
        cs_id = stable_id(f"customscript:{script_line}")
        cs_elem = ET.SubElement(root, "Element", {"Type": "CustomScript", "Id": cs_id})
        sc = ET.SubElement(cs_elem, "ScriptCode")
        sc.text = f"\n{script_line}\n"

    # 收集函数调用节点
    function_call_nodes: list[tuple[str, str]] = []  # (call_id, function_name)

    for trig in triggers:
        trig_id = stable_id(f"trigger:{trig['name']}")
        trig_elem = ET.SubElement(root, "Element", {"Type": "Trigger", "Id": trig_id})

        for idx, event_name in enumerate(trig["events"], start=1):
            call_id = stable_id(f"event:{trig['name']}:{idx}:{event_name}")
            ET.SubElement(trig_elem, "Event", {"Type": "FunctionCall", "Id": call_id})
            function_call_nodes.append((call_id, event_name))

        for idx, action_name in enumerate(trig["actions"], start=1):
            call_id = stable_id(f"action:{trig['name']}:{idx}:{action_name}")
            ET.SubElement(trig_elem, "Action", {"Type": "FunctionCall", "Id": call_id})
            function_call_nodes.append((call_id, action_name))

        if trig.get("script_action"):
            call_id = stable_id(f"scriptaction:{trig['name']}")
            ET.SubElement(trig_elem, "Action", {"Type": "FunctionCall", "Id": call_id})
            function_call_nodes.append((call_id, "__CUSTOM_SCRIPT__"))

    for call_id, func_name in function_call_nodes:
        fc_elem = ET.SubElement(root, "Element", {"Type": "FunctionCall", "Id": call_id})
        if func_name == "__CUSTOM_SCRIPT__":
            ET.SubElement(
                fc_elem,
                "FunctionDef",
                {"Type": "FunctionDef", "Library": "Ntve", "Id": NTVE_CUSTOM_SCRIPT_FUNCTION_DEF},
            )
            trig_name = call_id  # 占位，下面会按 call_id 反查
            # 通过 call_id 回找 script 文本
            script_text = ""
            for trig in triggers:
                expected = stable_id(f"scriptaction:{trig['name']}")
                if expected == call_id:
                    script_text = trig.get("script_action") or ""
                    break
            sc = ET.SubElement(fc_elem, "ScriptCode")
            sc.text = f"\n{script_text}\n" if script_text else ""
            continue

        fdef = NTVE_FUNCTION_MAP.get(func_name)
        if not fdef:
            continue
        ET.SubElement(fc_elem, "FunctionDef", {"Type": "FunctionDef", "Library": "Ntve", "Id": fdef})

    return ET.ElementTree(root)


def convert_one(mapscript_path: Path, output_path: Path) -> None:
    text = mapscript_path.read_text(encoding="utf-8", errors="ignore")
    triggers = parse_triggers_from_mapscript(text)
    custom_scripts = find_custom_script_includes(text)
    tree = build_trigger_xml(triggers, custom_scripts)
    ET.indent(tree, space="    ")
    tree.write(output_path, encoding="utf-8", xml_declaration=True, short_empty_elements=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="从 MapScript.galaxy 恢复 Triggers XML")
    parser.add_argument("mapscript", type=Path, help="MapScript.galaxy 路径")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="输出文件路径（默认：MapScript 同目录下的 Triggers.recovered）",
    )
    args = parser.parse_args()

    mapscript_path = args.mapscript
    if not mapscript_path.exists() or not mapscript_path.is_file():
        print(f"[ERROR] 文件不存在: {mapscript_path}", file=sys.stderr)
        return 1

    output_path = args.output or mapscript_path.with_name("Triggers.recovered")

    convert_one(mapscript_path, output_path)
    print(f"[OK] 已生成: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
