"""
export_chat.py — Claude Code / Codex / Kimi Code 会话导出脚本

用法：
  python export_chat.py <会话文件路径> <输出文件路径> [--tools] [--from-text 文本] [--topic 主题]
  python export_chat.py --cwd <当前工作目录> --marker <文本> [--marker <文本> ...] --output <输出文件路径> [--tools] [--from-text 文本] [--topic 主题]
  python export_chat.py --project-dir <项目会话目录> --marker <文本> [--marker <文本> ...] --output <输出文件路径> [--tools] [--from-text 文本] [--topic 主题]

默认用途：用当前会话中的 2-3 条独特文本作为 marker，先精确定位唯一 jsonl，再导出为 Markdown。
如果无法唯一定位，会打印少量候选会话摘要供人工确认；不会默认列出所有项目和所有会话。
"""
from archive_layout import ARCHIVE, BASE, REGISTRY, configured_zone, relative_from_archive, find_root

import argparse
import json
import os
import sys
from datetime import datetime
from output_guard import fresh_output, local_timestamp
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')


def extract_text(content, include_tools=False):
    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        texts = []
        for item in content:
            if not isinstance(item, dict):
                continue

            item_type = item.get('type')
            if item_type in ('text', 'input_text', 'output_text'):
                text = item.get('text', '').strip()
                if text:
                    texts.append(text)
            elif include_tools and item_type == 'tool_use':
                tool = item.get('name', '未知工具')
                inp = json.dumps(item.get('input', {}), ensure_ascii=False)
                if len(inp) > 200:
                    inp = inp[:200] + '...'
                texts.append(f'[调用工具：{tool}]\n参数：{inp}')
            elif include_tools and item_type == 'tool_result':
                result_content = item.get('content', '')
                result_text = extract_text(result_content, include_tools=False)
                if len(result_text) > 500:
                    result_text = result_text[:500] + '\n...(已截断)'
                texts.append(f'[工具返回结果]\n{result_text}')

        return '\n\n'.join(texts).strip()

    return str(content).strip()


def is_internal_user_context(text):
    """过滤 Codex Desktop 注入的工作区规则和运行环境，不把它们当作用户正文。"""
    stripped = text.lstrip()
    return (
        stripped.startswith('<recommended_plugins>')
        or stripped.startswith('# AGENTS.md instructions for ')
        or stripped.startswith('# AGENTS.md instructions\n')
        or stripped.startswith('# AGENTS.md instructions\r\n')
        or stripped.startswith('<environment_context>')
        or (stripped.startswith('<external_codex_apps_open_page>')
            and stripped.endswith('</external_codex_apps_open_page>'))
        or (stripped.startswith('<subagent_notification>')
            and stripped.endswith('</subagent_notification>'))
    )


def encode_project_path(path_str):
    return path_str.replace('\\', '-').replace('/', '-').replace(':', '-')


def resolve_project_dir(cwd=None, project_dir=None):
    if project_dir:
        resolved = Path(project_dir)
    elif cwd:
        encoded = encode_project_path(str(Path(cwd)))
        resolved = Path.home() / '.claude' / 'projects' / encoded
    else:
        raise ValueError('必须提供 --cwd 或 --project-dir')

    if not resolved.exists() or not resolved.is_dir():
        raise ValueError(f'项目会话目录不存在：{resolved}')
    return resolved


def find_workspace_root(start_path):
    return find_root(start_path)


def resolve_output_path(output_file, cwd=None, project_dir=None):
    output_path = Path(output_file)
    if output_path.is_absolute():
        return output_path

    base = find_workspace_root(cwd or Path.cwd())
    return base / output_path


def summarize_session(session_file):
    first_user_msg = ''
    timestamp = ''
    msg_count = 0

    with open(session_file, encoding='utf-8') as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            if obj.get('type') != 'user':
                continue

            msg_count += 1
            if first_user_msg:
                continue

            content = obj.get('message', {}).get('content', '')
            first_user_msg = extract_text(content, include_tools=False).replace('\n', ' ')[:80]
            timestamp = obj.get('timestamp', '')

    date_str = timestamp[:10] if timestamp else '未知日期'
    size = os.path.getsize(session_file)
    size_str = f'{size // 1024}KB' if size > 1024 else f'{size}B'
    return f'{session_file.name} | {date_str} | {msg_count}轮对话 | {size_str} | {first_user_msg}'


def locate_session_file(project_dir, markers):
    if not markers:
        raise ValueError('使用 --cwd / --project-dir 定位当前会话时，必须至少提供一个 --marker')

    candidates = []
    for session_file in sorted(project_dir.glob('*.jsonl')):
        try:
            raw = session_file.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        if all(marker in raw for marker in markers):
            candidates.append(session_file)

    if len(candidates) == 1:
        return candidates[0]

    if not candidates:
        marker_preview = ' | '.join(markers[:3])
        raise ValueError(f'未找到同时包含这些 marker 的会话：{marker_preview}')

    lines = ['无法唯一定位当前会话。候选如下：']
    for idx, candidate in enumerate(candidates[:5], 1):
        lines.append(f'{idx}. {summarize_session(candidate)}')
    raise ValueError('\n'.join(lines))


def is_kimi_wire(filepath):
    """Kimi Code 会话 wire.jsonl 首行是 metadata/protocol_version。"""
    try:
        with open(filepath, encoding='utf-8') as f:
            first = f.readline()
        obj = json.loads(first)
        return obj.get('type') == 'metadata' and 'protocol_version' in obj
    except (OSError, json.JSONDecodeError):
        return False


def parse_kimi_wire(filepath, include_tools=False, from_text=None):
    """解析 Kimi Code agents/main/wire.jsonl。

    用户正文取 context.append_message（role=user），只保留 origin.kind=user
    的消息（system-reminder、skill_activation 等内部注入一律过滤）；助手正文取
    content.part 中 type=text 的块，按 stepUuid 合并同一步的连续文本；
    think 块和工具调用默认不导出。
    """
    messages = []
    started = from_text is None

    with open(filepath, encoding='utf-8') as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts_ms = obj.get('time')
            ts = datetime.fromtimestamp(ts_ms / 1000).isoformat() if isinstance(ts_ms, (int, float)) else ''
            evt_type = obj.get('type')

            if evt_type == 'context.append_message':
                msg = obj.get('message', {})
                if msg.get('role') != 'user':
                    continue
                origin = (msg.get('origin') or {}).get('kind', 'user')
                if origin != 'user':
                    continue
                text = extract_text(msg.get('content', ''), include_tools=False)
                if not text:
                    continue
                if not started:
                    if from_text in text:
                        started = True
                    else:
                        continue
                messages.append({'role': 'User', 'text': text, 'time': ts})

            elif evt_type == 'context.append_loop_event':
                ev = obj.get('event', {})
                if not isinstance(ev, dict):
                    continue
                if not started:
                    part = ev.get('part') if ev.get('type') == 'content.part' else None
                    if from_text and isinstance(part, dict) and from_text in (part.get('text') or ''):
                        started = True
                    else:
                        continue
                if ev.get('type') == 'content.part':
                    part = ev.get('part', {})
                    if part.get('type') != 'text':
                        continue
                    text = part.get('text', '').strip()
                    if not text:
                        continue
                    step_key = ev.get('stepUuid') or ev.get('uuid')
                    if messages and messages[-1]['role'] == 'Assistant (Kimi)' and messages[-1].get('step') == step_key:
                        messages[-1]['text'] += '\n\n' + text
                    else:
                        messages.append({'role': 'Assistant (Kimi)', 'text': text, 'time': ts, 'step': step_key})
                elif include_tools and ev.get('type') == 'tool.call':
                    call = ev.get('call', {})
                    name = call.get('name', '未知工具')
                    inp = json.dumps(call.get('arguments', {}), ensure_ascii=False)
                    if len(inp) > 200:
                        inp = inp[:200] + '...'
                    messages.append({'role': 'Assistant (Kimi)', 'text': f'[调用工具：{name}]\n参数：{inp}', 'time': ts, 'step': ev.get('stepUuid')})

    if from_text and not started:
        raise ValueError(f'未找到起始文本：{from_text}')

    for msg in messages:
        msg.pop('step', None)
    return messages


def detect_models(filepath):
    """尽量从会话文件中提取实际使用的模型标识，按出现顺序去重。

    Kimi wire：config.update 事件的 modelAlias（如 kimi-code/k3）。
    Claude Code：助手消息的 message.model（如 claude-sonnet-4-5）。
    Codex rollout：turn_context 等 payload 中的 model（如 gpt-5-codex）。
    """
    models = []

    def add(value):
        if value and isinstance(value, str) and value not in models and value != '<synthetic>':
            models.append(value)

    try:
        with open(filepath, encoding='utf-8') as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                add(obj.get('modelAlias'))
                msg = obj.get('message')
                if isinstance(msg, dict):
                    add(msg.get('model'))
                payload = obj.get('payload')
                if isinstance(payload, dict):
                    add(payload.get('model'))
    except OSError:
        pass
    return models


def parse_session(filepath, include_tools=False, from_text=None, from_time=None):
    if is_kimi_wire(filepath):
        return parse_kimi_wire(filepath, include_tools=include_tools, from_text=from_text)

    messages = []
    started = from_text is None

    with open(filepath, encoding='utf-8') as f:
        for line in f:
            try:
                obj = json.loads(line.strip())
            except json.JSONDecodeError:
                continue

            timestamp = obj.get('timestamp', '')
            if from_time and (not timestamp or timestamp < from_time):
                continue

            msg_type = obj.get('type', '')
            msg = obj.get('message', {})
            source = 'claude'

            if msg_type == 'response_item':
                payload = obj.get('payload', {})
                if isinstance(payload, dict) and payload.get('type') == 'message':
                    msg = payload
                    source = 'codex'

            if not isinstance(msg, dict):
                continue

            role = msg.get('role', '')
            content = msg.get('content', '')
            text = extract_text(content, include_tools=include_tools)
            if not text:
                continue

            if role == 'user' and is_internal_user_context(text):
                continue

            if not started:
                if from_text in text:
                    started = True
                else:
                    continue

            if role == 'user':
                messages.append({
                    'role': 'User',
                    'text': text,
                    'time': timestamp
                })
            elif role == 'assistant':
                messages.append({
                    'role': 'Assistant (Codex)' if source == 'codex' else 'Assistant (Claude)',
                    'text': text,
                    'time': timestamp
                })

    if from_text and not started:
        raise ValueError(f'未找到起始文本：{from_text}')

    return messages


def generate_markdown(messages, session_file, topic=None, models=None):
    if not messages:
        return None

    first_time = local_timestamp(messages[0].get('time', ''))[:10] or '未知'
    last_time = local_timestamp(messages[-1].get('time', ''))[:10] or '未知'

    if any(msg['role'] == 'Assistant (Kimi)' for msg in messages):
        agent_name = 'Kimi Code'
    elif any(msg['role'] == 'Assistant (Codex)' for msg in messages):
        agent_name = 'Codex'
    elif any(msg['role'] == 'Assistant (DSH)' for msg in messages):
        agent_name = 'DeepSeek Harness'
    elif any(msg['role'] == 'Assistant (ZCode)' for msg in messages):
        agent_name = 'ZCode'
    else:
        agent_name = 'Claude Code'

    lines = []
    lines.append(f'# {agent_name} 对话记录')
    lines.append('')
    lines.append(f'- 导出时间：{datetime.now().strftime("%Y-%m-%d %H:%M")}')
    lines.append(f'- 对话时间：{first_time} ~ {last_time}')
    lines.append('- 消息时区：' + (str(configured_zone()) if configured_zone() else 'system local timezone'))
    lines.append(f'- 消息数量：{len(messages)} 条')
    lines.append(f'- 源文件：`{Path(session_file).name}`')
    model_str = '、'.join(models) if models else '未在会话文件中记录'
    lines.append(f'- 模型：{model_str}')
    if topic:
        lines.append(f'- 主题：{topic}')
    lines.append('')
    lines.append('---')
    lines.append('')

    for msg in messages:
        time_str = local_timestamp(msg.get('time', ''))
        lines.append(f'## {msg["role"]} {time_str}')
        lines.append('')
        lines.append(msg['text'])
        lines.append('')
        lines.append('---')
        lines.append('')

    return '\n'.join(lines)


def build_parser():
    parser = argparse.ArgumentParser(description='导出 Claude Code / Codex 会话为 Markdown')
    parser.add_argument('session_file', nargs='?')
    parser.add_argument('output_file', nargs='?')
    parser.add_argument('--tools', action='store_true', help='包含工具调用记录')
    parser.add_argument('--from-text', help='从首个包含该文本的消息开始导出')
    parser.add_argument('--from-time', help='从该 ISO 时间戳之后开始导出，例如 YYYY-MM-DDTHH:MM:SS+00:00')
    parser.add_argument('--topic', help='写入导出文件头部的主题')
    parser.add_argument('--cwd', help='当前工作目录；用于自动定位 ~/.claude/projects/<编码目录>')
    parser.add_argument('--project-dir', help='直接指定 Claude 会话目录')
    parser.add_argument('--marker', action='append', default=[], help='用于定位当前会话的独特文本，可重复传入')
    parser.add_argument('--output', help='与 --cwd / --project-dir 配合使用的输出路径')
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.cwd or args.project_dir:
            project_dir = resolve_project_dir(cwd=args.cwd, project_dir=args.project_dir)
            session_file = locate_session_file(project_dir, args.marker)
            output_file = args.output
            if not output_file:
                raise ValueError('使用 --cwd / --project-dir 时，必须提供 --output')
        else:
            session_file = args.session_file
            output_file = args.output_file
            if not session_file or not output_file:
                raise ValueError('用法：python export_chat.py <会话文件> <输出文件> [--tools] [--from-text 文本] [--from-time 时间] [--topic 主题]')
            session_file = Path(session_file)

        if not Path(session_file).exists():
            raise ValueError(f'会话文件不存在：{session_file}')

        messages = parse_session(
            session_file,
            include_tools=args.tools,
            from_text=args.from_text,
            from_time=args.from_time,
        )
        if not messages:
            print('该会话没有可导出的消息。')
            sys.exit(0)

        if len(args.marker) < 2 or any(not any(marker in m['text'] for m in messages) for marker in args.marker):
            raise ValueError('Supply at least two visible current-session --marker values')

        md = generate_markdown(messages, session_file, topic=args.topic, models=detect_models(session_file))
        output_path = fresh_output(resolve_output_path(output_file, cwd=args.cwd, project_dir=args.project_dir))
        if output_path.parent:
            output_path.parent.mkdir(parents=True, exist_ok=True)

        with output_path.open('x', encoding='utf-8') as handle:
            handle.write(md)
        size = output_path.stat().st_size
        size_str = f'{size // 1024}KB' if size > 1024 else f'{size}B'
        print(f'已导出 {len(messages)} 条消息 → {output_path}（{size_str}）')
    except ValueError as exc:
        print(str(exc))
        sys.exit(2)


if __name__ == '__main__':
    main()
