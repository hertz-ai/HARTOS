"""
HART OS CLI — Unified command-line interface for HARTOS.

AI-native OS: everything from coding to commerce, all from the command line.

Core:
    hart -p "task"                  Headless: execute any task and exit
    hart chat                       Interactive agent chat session
    hart status                     Service health check

Coding:
    hart code "task"                Coding via best available tool
    hart repomap                    Tree-sitter repository map

Desktop / Remote:
    hart desktop auto "instruction" VLM agentic loop (natural language)
    hart desktop click X Y          Direct left click at coordinates
    hart desktop type "hello"       Direct type text
    hart desktop key enter          Direct key press
    hart desktop hotkey "ctrl+c"    Direct hotkey combo
    hart desktop screenshot         Capture local screenshot
    hart desktop move X Y           Move cursor to coordinates
    hart desktop drag X1 Y1 X2 Y2  Drag from start to end
    hart desktop cursor             Get current cursor position
    hart desktop wait [seconds]     Wait/sleep
    hart remote "cmd" --url X       Remote execution via Nunba
    hart screenshot --url X         Remote screenshot via Nunba

Remote Desktop (RustDesk + Sunshine/Moonlight):
    hart remote-desktop             Show engine status
    hart remote-desktop id          Show this machine's Device ID
    hart remote-desktop host        Start hosting (show ID + password)
    hart remote-desktop connect <id> Connect to remote device
    hart remote-desktop sessions    List active sessions
    hart remote-desktop disconnect  End session (--all for all)
    hart remote-desktop transfer <f> <id>  Send file to device
    hart remote-desktop install <engine>   Show install instructions

Social:
    hart social post "text"         Create a post
    hart social feed                View feed
    hart social comment <id> "text" Comment on a post
    hart social communities         List communities
    hart social karma <user_id>     Check karma

Agents:
    hart agent list                 List agents/prompts
    hart agent create "goal"        Create agent with goal
    hart agent goal list            List active goals
    hart agent delegate "task"      Delegate to specialist

Expert Agents:
    hart expert list                List 96 expert agents
    hart expert find "task"         Find best expert for task

Channels:
    hart channel list               List configured channels
    hart channel send <ch> "msg"    Send message via channel

Payments / Commerce:
    hart pay request <amt> "desc"   Request payment
    hart pay list                   List payments
    hart pay authorize <id>         Authorize payment

MCP:
    hart mcp list                   List MCP servers & tools
    hart mcp execute <tool> '{}'    Execute MCP tool

Compute:
    hart compute status             Compute policy & resources
    hart compute join               Join peer compute network

Tools:
    hart tools list                 List installed coding tools
    hart tools install <name>       Install a coding tool

Recipes:
    hart recipe list                List saved recipes
    hart recipe show <id>           Show recipe details

Vision / Voice:
    hart vision describe "prompt"   Visual agent task
    hart voice transcribe <file>    Transcribe audio file

Entry point: hart (registered in setup.py / pyproject.toml)
"""
import json
import os
import sys

import click

# Default HARTOS server URL
DEFAULT_SERVER_URL = os.environ.get('HART_SERVER_URL', 'http://localhost:6777')
DEFAULT_NUNBA_URL = os.environ.get('HART_NUNBA_URL', 'http://localhost:6777')


@click.group(invoke_without_command=True)
@click.option('-p', '--prompt', default=None, help='Execute a single task headlessly and exit')
@click.option('--json', 'json_output', is_flag=True, help='Output results as JSON')
@click.option('--model', default='', help='LLM model override')
@click.option('--user-id', default='cli_user', help='User ID for agent session')
@click.option('--server', default=DEFAULT_SERVER_URL, help='HARTOS server URL')
@click.version_option(version='0.1.0', prog_name='hart')
@click.pass_context
def hart(ctx, prompt, json_output, model, user_id, server):
    """HART OS -- Hevolve Agentic Runtime CLI"""
    ctx.ensure_object(dict)
    ctx.obj['json_output'] = json_output
    ctx.obj['model'] = model
    ctx.obj['user_id'] = user_id
    ctx.obj['server'] = server

    if prompt:
        _execute_headless(prompt, user_id, model, json_output, server)
        return

    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


# ─── hart -p "task" — Headless mode ───


def _execute_headless(prompt, user_id, model, json_output, server_url):
    """Dispatch a task to /chat endpoint and print result."""
    import requests

    payload = {
        'user_id': user_id,
        'prompt_id': '0',
        'prompt': prompt,
    }
    if model:
        payload['model'] = model

    try:
        resp = requests.post(
            f'{server_url}/chat',
            json=payload,
            timeout=300,
        )
        data = resp.json()

        if json_output:
            click.echo(json.dumps(data, indent=2, default=str))
        else:
            # Extract the response text
            output = data.get('response', data.get('output', data.get('message', '')))
            if output:
                click.echo(output)
            else:
                click.echo(json.dumps(data, indent=2, default=str))

    except requests.ConnectionError:
        _error_exit(f'Cannot connect to HARTOS server at {server_url}', json_output)
    except Exception as e:
        _error_exit(str(e), json_output)


# ─── hart chat — Interactive chat ───


@hart.command()
@click.option('--prompt-id', default='0', help='Prompt ID for session')
@click.pass_context
def chat(ctx, prompt_id):
    """Interactive agent chat session."""
    import requests

    server = ctx.obj['server']
    user_id = ctx.obj['user_id']
    json_output = ctx.obj['json_output']

    click.echo(f'HART OS Chat — Connected to {server}')
    click.echo('Type "exit" or Ctrl+C to quit.\n')

    try:
        while True:
            try:
                user_input = click.prompt('You', prompt_suffix='> ')
            except click.Abort:
                break

            if user_input.lower() in ('exit', 'quit', '/exit', '/quit'):
                break

            if not user_input.strip():
                continue

            payload = {
                'user_id': user_id,
                'prompt_id': prompt_id,
                'prompt': user_input,
            }
            model = ctx.obj.get('model')
            if model:
                payload['model'] = model

            try:
                resp = requests.post(
                    f'{server}/chat',
                    json=payload,
                    timeout=300,
                )
                data = resp.json()
                output = data.get('response', data.get('output', data.get('message', '')))
                if output:
                    click.echo(f'\nAssistant: {output}\n')
                elif json_output:
                    click.echo(json.dumps(data, indent=2, default=str))
            except requests.ConnectionError:
                click.echo(f'\nError: Cannot connect to {server}\n')
            except Exception as e:
                click.echo(f'\nError: {e}\n')

    except KeyboardInterrupt:
        pass

    click.echo('\nGoodbye!')


# ─── hart code "task" — Coding via orchestrator ───


@hart.command()
@click.argument('task')
@click.option('--task-type', '-t', default='feature',
              type=click.Choice([
                  'code_review', 'feature', 'bug_fix', 'refactor',
                  'app_build', 'debugging', 'multi_file_edit', 'architecture',
              ]))
@click.option('--tool', default='', help='Force specific tool (aider_native, kilocode, claude_code, opencode)')
@click.option('--working-dir', '-d', default='.', help='Working directory for coding tool')
@click.option('--files', '-f', multiple=True, help='Files to include in context')
@click.pass_context
def code(ctx, task, task_type, tool, working_dir, files):
    """Execute a coding task via best available tool."""
    json_output = ctx.obj['json_output']
    user_id = ctx.obj['user_id']
    model = ctx.obj['model']

    try:
        from integrations.coding_agent.orchestrator import get_coding_orchestrator

        orchestrator = get_coding_orchestrator()
        result = orchestrator.execute(
            task=task,
            task_type=task_type,
            preferred_tool=tool,
            user_id=user_id,
            model=model,
            working_dir=working_dir,
        )

        if json_output:
            click.echo(json.dumps(result, indent=2, default=str))
        else:
            if result.get('success'):
                click.echo(f"Tool: {result.get('tool', 'unknown')}")
                click.echo(f"Time: {result.get('execution_time_s', 0)}s")
                click.echo(f"\n{result.get('output', '')}")
            else:
                click.echo(f"Error: {result.get('error', 'Unknown error')}", err=True)
                sys.exit(1)

    except ImportError as e:
        _error_exit(f'Coding agent not available: {e}', json_output)


# ─── hart desktop — Desktop automation (direct actions + VLM agentic loop) ───


def _desktop_action(action_dict, tier, json_output):
    """Execute a single desktop action and print result."""
    try:
        from integrations.vlm.local_computer_tool import execute_action
        result = execute_action(action_dict, tier)

        if json_output:
            click.echo(json.dumps(result, indent=2, default=str))
        else:
            if result.get('error'):
                click.echo(f"Error: {result['error']}", err=True)
                sys.exit(1)
            else:
                click.echo(result.get('output', 'Done'))
    except ImportError as e:
        _error_exit(f'Desktop action not available: {e}', json_output)


@hart.group(invoke_without_command=True)
@click.option('--remote', 'tier', flag_value='http', default=False,
              help='Route through HTTP (localhost:5001) instead of in-process')
@click.pass_context
def desktop(ctx, tier):
    """Desktop automation — direct pyautogui actions and VLM agentic loop.

    Direct actions:  hart desktop click 100 200
    Natural language: hart desktop auto "open chrome and search for news"
    """
    ctx.ensure_object(dict)
    ctx.obj['tier'] = 'http' if tier else 'inprocess'
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@desktop.command('auto')
@click.argument('instruction')
@click.option('--target', default='local', type=click.Choice(['local', 'remote']))
@click.option('--url', default=DEFAULT_NUNBA_URL, help='Nunba endpoint URL (for remote)')
@click.pass_context
def desktop_auto(ctx, instruction, target, url):
    """Execute desktop task via VLM agentic loop (natural language)."""
    json_output = ctx.obj['json_output']

    try:
        from integrations.coding_agent.remote_executor import RemoteDesktopExecutor

        executor = RemoteDesktopExecutor(nunba_url=url)
        result = executor.execute_desktop_task(instruction, target=target, nunba_url=url)

        if json_output:
            click.echo(json.dumps(result, indent=2, default=str))
        else:
            if result.get('success'):
                click.echo(result.get('output', 'Done'))
            else:
                click.echo(f"Error: {result.get('error', 'Unknown error')}", err=True)
                sys.exit(1)

    except ImportError as e:
        _error_exit(f'Desktop automation not available: {e}', json_output)


# ── Mouse actions ──

@desktop.command('click')
@click.argument('x', type=int)
@click.argument('y', type=int)
@click.pass_context
def desktop_click(ctx, x, y):
    """Left-click at screen coordinates."""
    _desktop_action({'action': 'left_click', 'coordinate': [x, y]},
                    ctx.obj['tier'], ctx.obj['json_output'])


@desktop.command('rightclick')
@click.argument('x', type=int)
@click.argument('y', type=int)
@click.pass_context
def desktop_rightclick(ctx, x, y):
    """Right-click at screen coordinates."""
    _desktop_action({'action': 'right_click', 'coordinate': [x, y]},
                    ctx.obj['tier'], ctx.obj['json_output'])


@desktop.command('doubleclick')
@click.argument('x', type=int)
@click.argument('y', type=int)
@click.pass_context
def desktop_doubleclick(ctx, x, y):
    """Double-click at screen coordinates."""
    _desktop_action({'action': 'double_click', 'coordinate': [x, y]},
                    ctx.obj['tier'], ctx.obj['json_output'])


@desktop.command('middleclick')
@click.argument('x', type=int)
@click.argument('y', type=int)
@click.pass_context
def desktop_middleclick(ctx, x, y):
    """Middle-click at screen coordinates."""
    _desktop_action({'action': 'middle_click', 'coordinate': [x, y]},
                    ctx.obj['tier'], ctx.obj['json_output'])


@desktop.command('move')
@click.argument('x', type=int)
@click.argument('y', type=int)
@click.pass_context
def desktop_move(ctx, x, y):
    """Move cursor to screen coordinates (hover)."""
    _desktop_action({'action': 'mouse_move', 'coordinate': [x, y]},
                    ctx.obj['tier'], ctx.obj['json_output'])


@desktop.command('drag')
@click.argument('x1', type=int)
@click.argument('y1', type=int)
@click.argument('x2', type=int)
@click.argument('y2', type=int)
@click.pass_context
def desktop_drag(ctx, x1, y1, x2, y2):
    """Left-click-drag from (x1,y1) to (x2,y2)."""
    _desktop_action({
        'action': 'left_click_drag',
        'startCoordinate': [x1, y1],
        'endCoordinate': [x2, y2],
        'coordinate': [x1, y1],
    }, ctx.obj['tier'], ctx.obj['json_output'])


@desktop.command('cursor')
@click.pass_context
def desktop_cursor(ctx):
    """Get current cursor position."""
    _desktop_action({'action': 'cursor_position'},
                    ctx.obj['tier'], ctx.obj['json_output'])


# ── Keyboard actions ──

@desktop.command('type')
@click.argument('text')
@click.pass_context
def desktop_type(ctx, text):
    """Type text at current cursor position."""
    _desktop_action({'action': 'type', 'text': text},
                    ctx.obj['tier'], ctx.obj['json_output'])


@desktop.command('key')
@click.argument('keyname')
@click.pass_context
def desktop_key(ctx, keyname):
    """Press a single key (enter, tab, escape, f1, space, etc.)."""
    _desktop_action({'action': 'key', 'text': keyname},
                    ctx.obj['tier'], ctx.obj['json_output'])


@desktop.command('hotkey')
@click.argument('combo')
@click.pass_context
def desktop_hotkey(ctx, combo):
    """Press a key combination (e.g. "ctrl+c", "alt+f4", "ctrl+shift+s")."""
    _desktop_action({'action': 'hotkey', 'text': combo},
                    ctx.obj['tier'], ctx.obj['json_output'])


# ── Screenshot ──

@desktop.command('screenshot')
@click.option('--save', default='', help='Save screenshot to file path')
@click.pass_context
def desktop_screenshot(ctx, save):
    """Capture a screenshot of the local screen."""
    json_output = ctx.obj['json_output']
    tier = ctx.obj['tier']

    try:
        from integrations.vlm.local_computer_tool import take_screenshot
        import base64

        b64_img = take_screenshot(tier)

        if save:
            img_data = base64.b64decode(b64_img)
            with open(save, 'wb') as f:
                f.write(img_data)
            click.echo(f'Screenshot saved to {save}')
        elif json_output:
            click.echo(json.dumps({'base64_image': b64_img[:100] + '...',
                                   'size_bytes': len(b64_img)}))
        else:
            click.echo(f'Screenshot captured ({len(b64_img)} bytes b64)')

    except ImportError as e:
        _error_exit(f'Screenshot not available: {e}', json_output)


# ── Wait ──

@desktop.command('wait')
@click.argument('seconds', type=float, default=2.0)
@click.pass_context
def desktop_wait(ctx, seconds):
    """Wait/sleep for N seconds (default: 2)."""
    _desktop_action({'action': 'wait', 'duration': seconds},
                    ctx.obj['tier'], ctx.obj['json_output'])


# ── File operations (via pyautogui action layer) ──

@desktop.command('ls')
@click.argument('path', default='.')
@click.pass_context
def desktop_ls(ctx, path):
    """List files and folders in a directory."""
    _desktop_action({'action': 'list_folders_and_files', 'path': path},
                    ctx.obj['tier'], ctx.obj['json_output'])


@desktop.command('readfile')
@click.argument('path')
@click.pass_context
def desktop_readfile(ctx, path):
    """Read and display file contents (up to 10KB)."""
    _desktop_action({'action': 'read_file_and_understand', 'path': path},
                    ctx.obj['tier'], ctx.obj['json_output'])


@desktop.command('writefile')
@click.argument('path')
@click.argument('content')
@click.pass_context
def desktop_writefile(ctx, path, content):
    """Write content to a file."""
    _desktop_action({'action': 'write_file', 'path': path, 'content': content},
                    ctx.obj['tier'], ctx.obj['json_output'])


@desktop.command('openfile')
@click.argument('path')
@click.pass_context
def desktop_openfile(ctx, path):
    """Open a file in the system's default GUI application."""
    _desktop_action({'action': 'open_file_gui', 'path': path},
                    ctx.obj['tier'], ctx.obj['json_output'])


@desktop.command('copyfile')
@click.argument('source')
@click.argument('destination')
@click.pass_context
def desktop_copyfile(ctx, source, destination):
    """Copy file from source to destination."""
    _desktop_action({
        'action': 'Open_file_and_copy_paste',
        'source_path': source,
        'destination_path': destination,
    }, ctx.obj['tier'], ctx.obj['json_output'])


# ─── hart remote "command" — Remote execution via Nunba ───


@hart.command()
@click.argument('command_text')
@click.option('--url', required=True, default=DEFAULT_NUNBA_URL, help='Remote Nunba endpoint URL')
@click.option('--timeout', default=120, help='Execution timeout in seconds')
@click.option('--force', is_flag=True, help='Bypass destructive command check')
@click.pass_context
def remote(ctx, command_text, url, timeout, force):
    """Execute a command on a remote machine via Nunba."""
    json_output = ctx.obj['json_output']

    try:
        from integrations.coding_agent.remote_executor import RemoteDesktopExecutor

        executor = RemoteDesktopExecutor(nunba_url=url)
        result = executor.execute(command_text, timeout=timeout, force=force)

        if json_output:
            click.echo(json.dumps(result, indent=2, default=str))
        else:
            if result.get('success'):
                click.echo(result.get('output', 'Done'))
            else:
                click.echo(f"Error: {result.get('error', 'Unknown error')}", err=True)
                sys.exit(1)

    except ImportError as e:
        _error_exit(f'Remote execution not available: {e}', json_output)


# ─── hart screenshot — Remote screenshot ───


@hart.command()
@click.option('--url', required=True, default=DEFAULT_NUNBA_URL, help='Remote Nunba endpoint URL')
@click.option('--save', default='', help='Save screenshot to file path')
@click.pass_context
def screenshot(ctx, url, save):
    """Capture screenshot from remote Nunba endpoint."""
    json_output = ctx.obj['json_output']

    try:
        from integrations.coding_agent.remote_executor import RemoteDesktopExecutor

        executor = RemoteDesktopExecutor(nunba_url=url)
        result = executor.screenshot()

        if result.get('success') and save:
            import base64
            img_data = base64.b64decode(result['image_base64'])
            with open(save, 'wb') as f:
                f.write(img_data)
            click.echo(f'Screenshot saved to {save}')
        elif json_output:
            click.echo(json.dumps(result, indent=2, default=str))
        elif result.get('success'):
            click.echo(f"Screenshot captured ({len(result.get('image_base64', ''))} bytes b64)")
        else:
            click.echo(f"Error: {result.get('error', 'Unknown error')}", err=True)

    except ImportError as e:
        _error_exit(f'Screenshot not available: {e}', json_output)


# ─── hart remote-desktop — Native remote desktop (RustDesk + Sunshine/Moonlight) ───


@hart.group('remote-desktop', invoke_without_command=True)
@click.pass_context
def remote_desktop(ctx):
    """Native remote desktop — RustDesk + Sunshine/Moonlight integration."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(remote_desktop_status)


@remote_desktop.command('id')
@click.pass_context
def remote_desktop_id(ctx):
    """Show this machine's device ID."""
    json_output = ctx.obj['json_output']
    try:
        from integrations.remote_desktop.device_id import get_device_id, format_device_id
        device_id = get_device_id()
        formatted = format_device_id(device_id)
        if json_output:
            click.echo(json.dumps({'device_id': device_id, 'formatted': formatted}))
        else:
            click.echo(f'Device ID: {formatted}')
    except Exception as e:
        _error_exit(f'Device ID unavailable: {e}', json_output)


@remote_desktop.command('status')
@click.pass_context
def remote_desktop_status(ctx):
    """Show remote desktop engine status (installed engines, sessions)."""
    json_output = ctx.obj['json_output']
    try:
        from integrations.remote_desktop.engine_selector import get_all_status
        status = get_all_status()
        if json_output:
            click.echo(json.dumps(status, indent=2, default=str))
        else:
            click.echo('Remote Desktop Engines:')
            for name, info in status.get('engines', {}).items():
                avail = 'available' if info.get('available') else 'not installed'
                click.echo(f'  {name:12s} {avail}')
            recs = status.get('install_recommendations', [])
            if recs:
                click.echo('\nInstall recommendations:')
                for rec in recs:
                    click.echo(f"  {rec['engine']:12s} {rec['reason']}")
                    click.echo(f"               {rec['install']}")
    except Exception as e:
        _error_exit(str(e), json_output)


@remote_desktop.command('host')
@click.option('--no-control', is_flag=True, help='View-only (no remote input)')
@click.option('--engine', type=click.Choice(['auto', 'rustdesk', 'sunshine', 'native']),
              default='auto', help='Engine to use')
@click.pass_context
def remote_desktop_host(ctx, no_control, engine):
    """Start hosting — share this screen. Shows Device ID + password."""
    json_output = ctx.obj['json_output']
    try:
        from integrations.remote_desktop.session_manager import (
            get_session_manager, SessionMode,
        )
        from integrations.remote_desktop.device_id import get_device_id, format_device_id

        device_id = get_device_id()
        sm = get_session_manager()
        mode = SessionMode.VIEW_ONLY if no_control else SessionMode.FULL_CONTROL
        password = sm.generate_otp(device_id)

        result = {
            'device_id': device_id,
            'formatted_id': format_device_id(device_id),
            'password': password,
            'mode': mode.value,
            'engine': engine,
        }

        # Start the chosen engine
        if engine in ('auto', 'rustdesk'):
            try:
                from integrations.remote_desktop.rustdesk_bridge import get_rustdesk_bridge
                bridge = get_rustdesk_bridge()
                if bridge.available:
                    bridge.set_password(password)
                    bridge.start_service()
                    rd_id = bridge.get_id()
                    if rd_id:
                        result['rustdesk_id'] = rd_id
                    result['engine'] = 'rustdesk'
            except Exception:
                pass

        if engine in ('auto', 'sunshine'):
            try:
                from integrations.remote_desktop.sunshine_bridge import get_sunshine_bridge
                bridge = get_sunshine_bridge()
                if bridge.available:
                    bridge.start_service()
                    result['sunshine_running'] = bridge.is_running()
                    if engine == 'sunshine':
                        result['engine'] = 'sunshine'
            except Exception:
                pass

        if json_output:
            click.echo(json.dumps(result, indent=2, default=str))
        else:
            click.echo(f"Device ID:  {result['formatted_id']}")
            click.echo(f"Password:   {password}")
            click.echo(f"Mode:       {mode.value}")
            click.echo(f"Engine:     {result.get('engine', 'native')}")
            if result.get('rustdesk_id'):
                click.echo(f"RustDesk ID: {result['rustdesk_id']}")
            click.echo('\nShare Device ID + Password with viewer to connect.')

    except Exception as e:
        _error_exit(str(e), json_output)


@remote_desktop.command('connect')
@click.argument('device_id')
@click.option('--password', prompt=True, hide_input=True, help='Access password')
@click.option('--mode', type=click.Choice(['full', 'view', 'file']),
              default='full', help='Connection mode')
@click.option('--engine', type=click.Choice(['auto', 'rustdesk', 'moonlight', 'native']),
              default='auto', help='Engine to use')
@click.pass_context
def remote_desktop_connect(ctx, device_id, password, mode, engine):
    """Connect to a remote device by Device ID."""
    json_output = ctx.obj['json_output']
    try:
        from integrations.remote_desktop.session_manager import SessionMode

        mode_map = {
            'full': SessionMode.FULL_CONTROL,
            'view': SessionMode.VIEW_ONLY,
            'file': SessionMode.FILE_TRANSFER,
        }
        session_mode = mode_map[mode]

        # Try RustDesk first
        if engine in ('auto', 'rustdesk'):
            try:
                from integrations.remote_desktop.rustdesk_bridge import get_rustdesk_bridge
                bridge = get_rustdesk_bridge()
                if bridge.available:
                    file_transfer = (session_mode == SessionMode.FILE_TRANSFER)
                    ok, msg = bridge.connect(device_id, password=password,
                                             file_transfer=file_transfer)
                    if ok:
                        result = {'success': True, 'engine': 'rustdesk',
                                  'device_id': device_id, 'message': msg}
                        if json_output:
                            click.echo(json.dumps(result, indent=2))
                        else:
                            click.echo(f'Connected via RustDesk: {msg}')
                        return
            except Exception:
                pass

        # Try Moonlight
        if engine in ('auto', 'moonlight'):
            try:
                from integrations.remote_desktop.sunshine_bridge import get_moonlight_bridge
                bridge = get_moonlight_bridge()
                if bridge.available:
                    ok, msg = bridge.stream(device_id)
                    if ok:
                        result = {'success': True, 'engine': 'moonlight',
                                  'device_id': device_id, 'message': msg}
                        if json_output:
                            click.echo(json.dumps(result, indent=2))
                        else:
                            click.echo(f'Streaming via Moonlight: {msg}')
                        return
            except Exception:
                pass

        _error_exit(f'No remote desktop engine available to connect to {device_id}',
                    json_output)

    except Exception as e:
        _error_exit(str(e), json_output)


@remote_desktop.command('sessions')
@click.pass_context
def remote_desktop_sessions(ctx):
    """List active remote desktop sessions."""
    json_output = ctx.obj['json_output']
    try:
        from integrations.remote_desktop.session_manager import get_session_manager
        sm = get_session_manager()
        sessions = sm.get_active_sessions()
        if json_output:
            click.echo(json.dumps([
                {'session_id': s.session_id, 'host_device_id': s.host_device_id,
                 'mode': s.mode.value, 'state': s.state.value,
                 'viewers': s.viewer_device_ids}
                for s in sessions
            ], indent=2, default=str))
        else:
            if not sessions:
                click.echo('No active sessions.')
            else:
                click.echo(f'Active sessions ({len(sessions)}):')
                for s in sessions:
                    click.echo(f'  {s.session_id[:8]}  host={s.host_device_id[:12]}  '
                               f'mode={s.mode.value}  state={s.state.value}  '
                               f'viewers={len(s.viewer_device_ids)}')
    except Exception as e:
        _error_exit(str(e), json_output)


@remote_desktop.command('disconnect')
@click.argument('session_id', required=False)
@click.option('--all', 'disconnect_all', is_flag=True, help='Disconnect all sessions')
@click.pass_context
def remote_desktop_disconnect(ctx, session_id, disconnect_all):
    """End a remote desktop session."""
    json_output = ctx.obj['json_output']
    try:
        from integrations.remote_desktop.session_manager import get_session_manager
        sm = get_session_manager()

        if disconnect_all:
            sessions = sm.get_active_sessions()
            for s in sessions:
                sm.disconnect_session(s.session_id)
            if json_output:
                click.echo(json.dumps({'disconnected': len(sessions)}))
            else:
                click.echo(f'Disconnected {len(sessions)} session(s).')
        elif session_id:
            sm.disconnect_session(session_id)
            if json_output:
                click.echo(json.dumps({'disconnected': session_id}))
            else:
                click.echo(f'Disconnected session {session_id[:8]}.')
        else:
            _error_exit('Provide a session ID or use --all', json_output)

    except Exception as e:
        _error_exit(str(e), json_output)


@remote_desktop.command('transfer')
@click.argument('file_path', type=click.Path(exists=True))
@click.argument('device_id')
@click.pass_context
def remote_desktop_transfer(ctx, file_path, device_id):
    """Send a file to a remote device via RustDesk file transfer."""
    json_output = ctx.obj['json_output']
    try:
        from integrations.remote_desktop.rustdesk_bridge import get_rustdesk_bridge
        bridge = get_rustdesk_bridge()
        if not bridge.available:
            _error_exit('RustDesk not installed (required for file transfer)', json_output)

        ok, msg = bridge.connect(device_id, file_transfer=True)
        if ok:
            if json_output:
                click.echo(json.dumps({'success': True, 'message': msg}))
            else:
                click.echo(f'File transfer session opened: {msg}')
        else:
            _error_exit(f'Failed to open file transfer: {msg}', json_output)

    except Exception as e:
        _error_exit(str(e), json_output)


@remote_desktop.command('install')
@click.argument('engine', type=click.Choice(['rustdesk', 'sunshine', 'moonlight']))
@click.pass_context
def remote_desktop_install(ctx, engine):
    """Show install command for a remote desktop engine."""
    json_output = ctx.obj['json_output']
    try:
        if engine == 'rustdesk':
            from integrations.remote_desktop.rustdesk_bridge import RustDeskBridge
            cmd = RustDeskBridge().get_install_command()
        elif engine == 'sunshine':
            from integrations.remote_desktop.sunshine_bridge import SunshineBridge
            cmd = SunshineBridge().get_install_command()
        elif engine == 'moonlight':
            from integrations.remote_desktop.sunshine_bridge import MoonlightBridge
            cmd = MoonlightBridge().get_install_command()
        else:
            cmd = ''

        if json_output:
            click.echo(json.dumps({'engine': engine, 'install_command': cmd}))
        else:
            click.echo(f'Install {engine}:\n{cmd}')

    except Exception as e:
        _error_exit(str(e), json_output)


# ─── hart tools — Tool management ───


@hart.group()
def tools():
    """Manage coding tools (list, install)."""
    pass


@tools.command('list')
@click.pass_context
def tools_list(ctx):
    """List all coding tools with install status and benchmarks."""
    json_output = ctx.obj['json_output']

    try:
        from integrations.coding_agent.installer import get_tool_info
        from integrations.coding_agent.benchmark_tracker import get_benchmark_tracker

        info = get_tool_info()
        try:
            benchmarks = get_benchmark_tracker().get_summary()
        except Exception:
            benchmarks = None

        if json_output:
            click.echo(json.dumps({'tools': info, 'benchmarks': benchmarks}, indent=2, default=str))
        else:
            click.echo('Coding Tools:')
            click.echo('-' * 60)
            for name, data in info.items():
                status = 'installed' if data['installed'] else 'not installed'
                tool_type = data.get('type', 'subprocess')
                click.echo(f"  {name:16} [{tool_type:10}] {status}")
                if data.get('version'):
                    click.echo(f"    {'':16} version: {data['version']}")
            click.echo('')
            if benchmarks and benchmarks.get('total_benchmarks', 0) > 0:
                click.echo(f"Benchmarks: {benchmarks['total_benchmarks']} recorded")

    except ImportError as e:
        _error_exit(f'Coding agent not available: {e}', json_output)


@tools.command('install')
@click.argument('tool_name')
@click.pass_context
def tools_install(ctx, tool_name):
    """Install a coding tool."""
    json_output = ctx.obj['json_output']

    try:
        from integrations.coding_agent.installer import install_tool

        result = install_tool(tool_name)

        if json_output:
            click.echo(json.dumps(result, indent=2, default=str))
        else:
            if result.get('success'):
                click.echo(result.get('message', f'{tool_name} installed'))
            else:
                click.echo(f"Error: {result.get('error', 'Installation failed')}", err=True)

    except ImportError as e:
        _error_exit(f'Installer not available: {e}', json_output)


# ─── hart recipe — Recipe management ───


@hart.group()
def recipe():
    """Manage saved recipes (list, show)."""
    pass


@recipe.command('list')
@click.pass_context
def recipe_list(ctx):
    """List all saved recipes."""
    json_output = ctx.obj['json_output']
    import glob

    recipe_files = sorted(glob.glob('prompts/*_recipe.json'))

    if json_output:
        recipes = []
        for f in recipe_files:
            try:
                with open(f) as fh:
                    data = json.load(fh)
                recipes.append({'file': f, 'prompt_id': data.get('prompt_id', ''), 'flows': len(data.get('flows', []))})
            except Exception:
                recipes.append({'file': f, 'error': 'parse failed'})
        click.echo(json.dumps(recipes, indent=2))
    else:
        if not recipe_files:
            click.echo('No recipes found in prompts/')
            return
        click.echo(f'Recipes ({len(recipe_files)}):')
        for f in recipe_files:
            name = os.path.basename(f).replace('_recipe.json', '')
            click.echo(f'  {name}')


@recipe.command('show')
@click.argument('recipe_id')
@click.pass_context
def recipe_show(ctx, recipe_id):
    """Show details of a saved recipe."""
    json_output = ctx.obj['json_output']
    import glob

    # Find matching recipe file
    matches = glob.glob(f'prompts/{recipe_id}*_recipe.json')
    if not matches:
        _error_exit(f'Recipe not found: {recipe_id}', json_output)
        return

    try:
        with open(matches[0]) as f:
            data = json.load(f)

        if json_output:
            click.echo(json.dumps(data, indent=2, default=str))
        else:
            click.echo(f"Recipe: {matches[0]}")
            click.echo(f"Prompt ID: {data.get('prompt_id', 'N/A')}")
            flows = data.get('flows', [])
            click.echo(f"Flows: {len(flows)}")
            for i, flow in enumerate(flows):
                actions = flow.get('actions', [])
                click.echo(f"  Flow {i}: {len(actions)} action(s)")
                for j, action in enumerate(actions):
                    click.echo(f"    Action {j}: {action.get('steps', 'N/A')[:80]}")

    except Exception as e:
        _error_exit(f'Error reading recipe: {e}', json_output)


# ─── hart status — Service health check ───


@hart.command()
@click.option('--server', default=DEFAULT_SERVER_URL, help='HARTOS server URL')
@click.pass_context
def status(ctx, server):
    """Check HARTOS service health."""
    json_output = ctx.obj['json_output']
    server = server or ctx.obj['server']

    import requests

    try:
        resp = requests.get(f'{server}/status', timeout=10)
        data = resp.json()

        if json_output:
            click.echo(json.dumps(data, indent=2, default=str))
        else:
            click.echo(f"Server: {server}")
            click.echo(f"Status: {data.get('status', 'unknown')}")
            for key, val in data.items():
                if key != 'status':
                    click.echo(f"  {key}: {val}")

    except requests.ConnectionError:
        if json_output:
            click.echo(json.dumps({'status': 'offline', 'server': server}))
        else:
            click.echo(f"Server: {server}")
            click.echo("Status: OFFLINE (cannot connect)")
        sys.exit(1)
    except Exception as e:
        _error_exit(str(e), json_output)


# ─── hart repomap — Repo map utility ───


@hart.command()
@click.option('--dir', '-d', 'working_dir', default='.', help='Directory to map')
@click.option('--tokens', default=2048, help='Max tokens for the map')
@click.pass_context
def repomap(ctx, working_dir, tokens):
    """Generate a tree-sitter based repository map."""
    json_output = ctx.obj['json_output']

    try:
        from integrations.coding_agent.aider_native_backend import AiderNativeBackend

        backend = AiderNativeBackend()
        if not backend.is_installed():
            _error_exit('Aider core not installed', json_output)
            return

        repo_map = backend.get_repo_map(working_dir=working_dir, max_tokens=tokens)

        if json_output:
            click.echo(json.dumps({'repo_map': repo_map, 'tokens': tokens}, default=str))
        else:
            if repo_map:
                click.echo(repo_map)
            else:
                click.echo('No source files found to map.')

    except ImportError as e:
        _error_exit(f'Repo map not available: {e}', json_output)


# ─── hart social — Social platform ───


@hart.group()
@click.pass_context
def social(ctx):
    """Social platform: posts, comments, communities, feed, karma."""
    pass


@social.command('post')
@click.argument('text')
@click.option('--community', '-c', default='', help='Community to post in')
@click.option('--title', '-t', default='', help='Post title')
@click.pass_context
def social_post(ctx, text, community, title):
    """Create a new post."""
    server = ctx.obj['server']
    user_id = ctx.obj['user_id']
    json_output = ctx.obj['json_output']

    payload = {'content': text, 'author_id': user_id}
    if community:
        payload['community'] = community
    if title:
        payload['title'] = title

    result = _api_post(f'{server}/api/posts', payload, json_output)
    if not json_output and result:
        click.echo(f"Post created: {result.get('id', 'OK')}")


@social.command('feed')
@click.option('--type', 'feed_type', default='all',
              type=click.Choice(['all', 'trending', 'agents']))
@click.option('--limit', default=20, help='Number of posts')
@click.pass_context
def social_feed(ctx, feed_type, limit):
    """View your feed."""
    server = ctx.obj['server']
    json_output = ctx.obj['json_output']
    user_id = ctx.obj['user_id']

    url = f'{server}/api/feed/{feed_type}?user_id={user_id}&limit={limit}'
    result = _api_get(url, json_output)
    if not json_output and result:
        posts = result if isinstance(result, list) else result.get('posts', [])
        if not posts:
            click.echo('No posts in feed.')
            return
        for p in posts[:limit]:
            author = p.get('author', p.get('author_id', '?'))
            content = p.get('content', '')[:120]
            click.echo(f"  [{p.get('id', '?')}] {author}: {content}")


@social.command('comment')
@click.argument('post_id')
@click.argument('text')
@click.pass_context
def social_comment(ctx, post_id, text):
    """Comment on a post."""
    server = ctx.obj['server']
    user_id = ctx.obj['user_id']
    json_output = ctx.obj['json_output']

    payload = {'content': text, 'author_id': user_id}
    result = _api_post(f'{server}/api/posts/{post_id}/comments', payload, json_output)
    if not json_output and result:
        click.echo(f"Comment added to post {post_id}")


@social.command('vote')
@click.argument('post_id')
@click.option('--down', is_flag=True, help='Downvote instead of upvote')
@click.pass_context
def social_vote(ctx, post_id, down):
    """Upvote or downvote a post."""
    server = ctx.obj['server']
    user_id = ctx.obj['user_id']
    json_output = ctx.obj['json_output']

    direction = 'downvote' if down else 'upvote'
    payload = {'user_id': user_id}
    result = _api_post(f'{server}/api/posts/{post_id}/{direction}', payload, json_output)
    if not json_output and result:
        click.echo(f"{'Downvoted' if down else 'Upvoted'} post {post_id}")


@social.command('communities')
@click.option('--limit', default=20, help='Number of communities')
@click.pass_context
def social_communities(ctx, limit):
    """List communities."""
    server = ctx.obj['server']
    json_output = ctx.obj['json_output']

    result = _api_get(f'{server}/api/communities?limit={limit}', json_output)
    if not json_output and result:
        communities = result if isinstance(result, list) else result.get('communities', [])
        if not communities:
            click.echo('No communities found.')
            return
        for c in communities:
            name = c.get('name', '?')
            members = c.get('member_count', '?')
            click.echo(f"  {name} ({members} members)")


@social.command('karma')
@click.argument('user_id', required=False)
@click.pass_context
def social_karma(ctx, user_id):
    """Check karma / resonance score for a user."""
    server = ctx.obj['server']
    json_output = ctx.obj['json_output']
    uid = user_id or ctx.obj['user_id']

    result = _api_get(f'{server}/api/users/{uid}/karma', json_output)
    if not json_output and result:
        click.echo(f"User: {uid}")
        click.echo(f"Karma: {result.get('karma', result.get('score', '?'))}")


@social.command('encounter')
@click.option('--nearby', is_flag=True, help='Show nearby encounters')
@click.pass_context
def social_encounter(ctx, nearby):
    """View encounters and proximity matches."""
    server = ctx.obj['server']
    user_id = ctx.obj['user_id']
    json_output = ctx.obj['json_output']

    endpoint = 'nearby' if nearby else user_id
    result = _api_get(f'{server}/api/social/encounters/{endpoint}', json_output)
    if not json_output and result:
        encounters = result if isinstance(result, list) else result.get('encounters', [])
        if not encounters:
            click.echo('No encounters found.')
            return
        for e in encounters:
            click.echo(f"  [{e.get('id', '?')}] {e.get('type', '?')}: {e.get('description', '')[:80]}")


@social.command('wallet')
@click.pass_context
def social_wallet(ctx):
    """View resonance wallet balance and transactions."""
    server = ctx.obj['server']
    user_id = ctx.obj['user_id']
    json_output = ctx.obj['json_output']

    result = _api_get(f'{server}/api/social/resonance/wallet/{user_id}', json_output)
    if not json_output and result:
        click.echo(f"Balance: {result.get('balance', '?')} Spark")
        click.echo(f"Level: {result.get('level', '?')}")
        click.echo(f"Streak: {result.get('streak', '?')} days")


@social.command('leaderboard')
@click.option('--type', 'lb_type', default='resonance',
              type=click.Choice(['resonance', 'agents', 'hosting']))
@click.pass_context
def social_leaderboard(ctx, lb_type):
    """View leaderboard."""
    server = ctx.obj['server']
    json_output = ctx.obj['json_output']

    urls = {
        'resonance': f'{server}/api/social/resonance/leaderboard',
        'agents': f'{server}/api/social/agents/leaderboard',
        'hosting': f'{server}/api/social/hosting/leaderboard',
    }
    result = _api_get(urls[lb_type], json_output)
    if not json_output and result:
        entries = result if isinstance(result, list) else result.get('leaderboard', [])
        click.echo(f'{lb_type.title()} Leaderboard:')
        for i, e in enumerate(entries[:20], 1):
            name = e.get('name', e.get('user_id', '?'))
            score = e.get('score', e.get('balance', '?'))
            click.echo(f"  {i:3}. {name}: {score}")


# ─── hart agent — Agent management ───


@hart.group()
@click.pass_context
def agent(ctx):
    """Manage agents: create, list, delegate, goals."""
    pass


@agent.command('list')
@click.pass_context
def agent_list(ctx):
    """List available agents/prompts."""
    server = ctx.obj['server']
    user_id = ctx.obj['user_id']
    json_output = ctx.obj['json_output']

    result = _api_get(f'{server}/prompts?user_id={user_id}', json_output)
    if not json_output and result:
        prompts = result if isinstance(result, list) else result.get('prompts', [])
        if not prompts:
            click.echo('No agents/prompts found.')
            return
        click.echo(f'Agents ({len(prompts)}):')
        for p in prompts:
            pid = p.get('prompt_id', p.get('id', '?'))
            name = p.get('name', p.get('prompt', ''))[:60]
            click.echo(f"  [{pid}] {name}")


@agent.command('create')
@click.argument('goal')
@click.option('--type', 'goal_type', default='feature', help='Goal type')
@click.pass_context
def agent_create(ctx, goal, goal_type):
    """Create an agent with a goal (dispatches to /chat with create_agent=true)."""
    server = ctx.obj['server']
    user_id = ctx.obj['user_id']
    model = ctx.obj['model']
    json_output = ctx.obj['json_output']

    payload = {
        'user_id': user_id,
        'prompt_id': '0',
        'prompt': goal,
        'create_agent': True,
    }
    if model:
        payload['model'] = model

    result = _api_post(f'{server}/chat', payload, json_output)
    if not json_output and result:
        output = result.get('response', result.get('output', result.get('message', '')))
        if output:
            click.echo(output)
        else:
            click.echo(json.dumps(result, indent=2, default=str))


@agent.command('delegate')
@click.argument('task')
@click.option('--specialist', default='', help='Specialist type to delegate to')
@click.pass_context
def agent_delegate(ctx, task, specialist):
    """Delegate a task to a specialist agent."""
    json_output = ctx.obj['json_output']

    try:
        from integrations.internal_comm.skill_registry import get_skill_registry
        from integrations.internal_comm.delegation import create_delegation_function

        registry = get_skill_registry()
        delegate_fn = create_delegation_function('cli_user', registry)
        result = delegate_fn(task, specialist_type=specialist)

        if json_output:
            click.echo(json.dumps({'result': result}, default=str))
        else:
            click.echo(result)

    except ImportError as e:
        _error_exit(f'Delegation not available: {e}', json_output)


@agent.group('goal')
@click.pass_context
def agent_goal(ctx):
    """Agent goal management."""
    pass


@agent_goal.command('list')
@click.option('--type', 'goal_type', default=None, help='Filter by goal type')
@click.option('--status', default=None, help='Filter by status')
@click.pass_context
def agent_goal_list(ctx, goal_type, status):
    """List active goals."""
    json_output = ctx.obj['json_output']

    try:
        from integrations.agent_engine.goal_manager import AgentGoal
        from integrations.social.models import db_session

        with db_session() as db:
            goals = AgentGoal.list_goals(db, goal_type=goal_type, status=status)

        if json_output:
            click.echo(json.dumps(goals, indent=2, default=str))
        else:
            if not goals:
                click.echo('No goals found.')
                return
            click.echo(f'Goals ({len(goals)}):')
            for g in goals:
                click.echo(f"  [{g.get('id', '?')}] {g.get('goal_type', '?')}: "
                           f"{g.get('title', '')[:60]} ({g.get('status', '?')})")

    except ImportError as e:
        _error_exit(f'Goal manager not available: {e}', json_output)


@agent_goal.command('create')
@click.argument('title')
@click.option('--type', 'goal_type', default='coding',
              help='Goal type (coding, marketing, trading, finance, etc.)')
@click.option('--description', '-d', default='', help='Goal description')
@click.pass_context
def agent_goal_create(ctx, title, goal_type, description):
    """Create a new agent goal."""
    json_output = ctx.obj['json_output']

    try:
        from integrations.agent_engine.goal_manager import AgentGoal
        from integrations.social.models import db_session

        with db_session() as db:
            result = AgentGoal.create_goal(
                db, goal_type=goal_type, title=title, description=description)

        if json_output:
            click.echo(json.dumps(result, indent=2, default=str))
        else:
            click.echo(f"Goal created: {result.get('id', 'OK')}")

    except ImportError as e:
        _error_exit(f'Goal manager not available: {e}', json_output)


@agent_goal.command('types')
@click.pass_context
def agent_goal_types(ctx):
    """List registered goal types."""
    json_output = ctx.obj['json_output']

    try:
        from integrations.agent_engine.goal_manager import get_registered_types
        types = get_registered_types()

        if json_output:
            click.echo(json.dumps({'goal_types': types}))
        else:
            click.echo('Registered goal types:')
            for t in types:
                click.echo(f'  - {t}')

    except ImportError as e:
        _error_exit(f'Goal manager not available: {e}', json_output)


# ─── hart expert — Expert agents ───


@hart.group()
@click.pass_context
def expert(ctx):
    """96 expert agents: find, list, get info."""
    pass


@expert.command('list')
@click.option('--category', '-c', default=None, help='Filter by category')
@click.pass_context
def expert_list(ctx, category):
    """List available expert agents."""
    json_output = ctx.obj['json_output']

    try:
        from integrations.expert_agents.registry import EXPERT_REGISTRY

        agents = EXPERT_REGISTRY
        if category:
            agents = {k: v for k, v in agents.items()
                      if v.category.value.lower() == category.lower()}

        if json_output:
            click.echo(json.dumps(
                {k: {'name': v.name, 'category': v.category.value,
                     'description': v.description}
                 for k, v in agents.items()}, indent=2))
        else:
            click.echo(f'Expert Agents ({len(agents)}):')
            for aid, a in agents.items():
                click.echo(f"  {aid:30} [{a.category.value:20}] {a.name}")

    except ImportError as e:
        _error_exit(f'Expert agents not available: {e}', json_output)


@expert.command('find')
@click.argument('task')
@click.option('--top', default=5, help='Number of results')
@click.pass_context
def expert_find(ctx, task, top):
    """Find the best expert agent for a task."""
    json_output = ctx.obj['json_output']

    try:
        from integrations.expert_agents.registry import recommend_experts_for_dream

        experts = recommend_experts_for_dream(task, top_k=top)

        if json_output:
            click.echo(json.dumps(
                [{'agent_id': e.agent_id, 'name': e.name,
                  'category': e.category.value, 'description': e.description}
                 for e in experts], indent=2))
        else:
            click.echo(f'Top {len(experts)} experts for: "{task}"')
            for i, e in enumerate(experts, 1):
                click.echo(f"  {i}. {e.name} ({e.category.value})")
                click.echo(f"     {e.description[:80]}")

    except ImportError as e:
        _error_exit(f'Expert agents not available: {e}', json_output)


@expert.command('info')
@click.argument('agent_id')
@click.pass_context
def expert_info(ctx, agent_id):
    """Get detailed info about an expert agent."""
    json_output = ctx.obj['json_output']

    try:
        from integrations.expert_agents.registry import get_expert_info

        info = get_expert_info(agent_id)
        if not info:
            _error_exit(f'Expert not found: {agent_id}', json_output)
            return

        if json_output:
            click.echo(json.dumps(info, indent=2, default=str))
        else:
            click.echo(f"Agent: {info.get('name', agent_id)}")
            click.echo(f"Category: {info.get('category', '?')}")
            click.echo(f"Description: {info.get('description', '?')}")
            caps = info.get('capabilities', [])
            if caps:
                click.echo(f"Capabilities: {', '.join(str(c) for c in caps)}")
            click.echo(f"Model: {info.get('model_type', '?')}")
            click.echo(f"Cost: {info.get('cost_per_call', '?')} per call")
            click.echo(f"Reliability: {info.get('reliability', '?')}")

    except ImportError as e:
        _error_exit(f'Expert agents not available: {e}', json_output)


# ─── hart channel — Channel management ───


@hart.group()
@click.pass_context
def channel(ctx):
    """Channel adapters: Telegram, Discord, Slack, WhatsApp, Signal, etc."""
    pass


@channel.command('list')
@click.pass_context
def channel_list(ctx):
    """List configured channel adapters."""
    server = ctx.obj['server']
    json_output = ctx.obj['json_output']

    # Try admin API first, fall back to import
    result = _api_get(f'{server}/api/admin/channels', json_output, silent=True)
    if result:
        if not json_output:
            channels = result if isinstance(result, list) else result.get('channels', [])
            click.echo(f'Channels ({len(channels)}):')
            for ch in channels:
                name = ch.get('name', ch.get('type', '?'))
                status = ch.get('status', '?')
                click.echo(f"  {name:20} [{status}]")
        return

    # Fallback: import registry
    try:
        from integrations.channels.registry import ChannelRegistry

        registry = ChannelRegistry.__instance__ if hasattr(ChannelRegistry, '__instance__') else None
        if registry and hasattr(registry, 'adapters'):
            if json_output:
                click.echo(json.dumps(
                    {name: {'type': type(a).__name__}
                     for name, a in registry.adapters.items()}, indent=2))
            else:
                click.echo(f'Channels ({len(registry.adapters)}):')
                for name, adapter in registry.adapters.items():
                    click.echo(f"  {name:20} [{type(adapter).__name__}]")
        else:
            click.echo('Supported: telegram, discord, slack, whatsapp, signal, google_chat, imessage, web')

    except ImportError:
        click.echo('Supported: telegram, discord, slack, whatsapp, signal, google_chat, imessage, web')


@channel.command('send')
@click.argument('channel_name')
@click.argument('message')
@click.option('--chat-id', required=True, help='Target chat/channel ID')
@click.pass_context
def channel_send(ctx, channel_name, message, chat_id):
    """Send a message through a channel adapter."""
    server = ctx.obj['server']
    json_output = ctx.obj['json_output']

    payload = {
        'channel': channel_name,
        'chat_id': chat_id,
        'message': message,
    }
    result = _api_post(f'{server}/api/admin/channels/send', payload, json_output)
    if not json_output and result:
        click.echo(f"Sent to {channel_name}:{chat_id}")


@channel.command('broadcast')
@click.argument('message')
@click.option('--channels', '-c', multiple=True, help='Channels to broadcast to (all if empty)')
@click.pass_context
def channel_broadcast(ctx, message, channels):
    """Broadcast a message to multiple channels."""
    server = ctx.obj['server']
    json_output = ctx.obj['json_output']

    payload = {
        'message': message,
        'channels': list(channels) if channels else [],
    }
    result = _api_post(f'{server}/api/admin/channels/broadcast', payload, json_output)
    if not json_output and result:
        click.echo(f"Broadcast sent to {len(channels) or 'all'} channels")


# ─── hart pay — Payments / AP2 commerce ───


@hart.group()
@click.pass_context
def pay(ctx):
    """Payments and agentic commerce (AP2 protocol)."""
    pass


@pay.command('request')
@click.argument('amount', type=float)
@click.argument('description')
@click.option('--currency', default='USD', help='Currency code')
@click.option('--method', default='internal_credits',
              type=click.Choice(['internal_credits', 'stripe', 'paypal']))
@click.pass_context
def pay_request(ctx, amount, description, currency, method):
    """Request a payment."""
    json_output = ctx.obj['json_output']
    user_id = ctx.obj['user_id']

    try:
        from integrations.ap2.ap2_protocol import get_payment_ledger, PaymentMethod

        ledger = get_payment_ledger()
        method_enum = PaymentMethod(method)
        payment = ledger.create_payment_request(
            amount=amount, currency=currency, description=description,
            requester_agent_id=user_id, payment_method=method_enum,
        )

        if json_output:
            click.echo(json.dumps(payment.to_dict(), indent=2, default=str))
        else:
            click.echo(f"Payment requested: {payment.payment_id}")
            click.echo(f"  Amount: {amount} {currency}")
            click.echo(f"  Status: {payment.status.value}")

    except ImportError as e:
        _error_exit(f'AP2 not available: {e}', json_output)


@pay.command('list')
@click.option('--status', default=None, help='Filter by status')
@click.pass_context
def pay_list(ctx, status):
    """List payments."""
    json_output = ctx.obj['json_output']
    user_id = ctx.obj['user_id']

    try:
        from integrations.ap2.ap2_protocol import get_payment_ledger

        ledger = get_payment_ledger()
        payments = ledger.list_payments(agent_id=user_id)

        if json_output:
            click.echo(json.dumps(
                [p.to_dict() for p in payments], indent=2, default=str))
        else:
            if not payments:
                click.echo('No payments found.')
                return
            click.echo(f'Payments ({len(payments)}):')
            for p in payments:
                click.echo(f"  [{p.payment_id[:12]}] {p.amount} {p.currency} "
                           f"- {p.status.value} - {p.description[:40]}")

    except ImportError as e:
        _error_exit(f'AP2 not available: {e}', json_output)


@pay.command('authorize')
@click.argument('payment_id')
@click.pass_context
def pay_authorize(ctx, payment_id):
    """Authorize a pending payment."""
    json_output = ctx.obj['json_output']
    user_id = ctx.obj['user_id']

    try:
        from integrations.ap2.ap2_protocol import get_payment_ledger

        ledger = get_payment_ledger()
        success = ledger.authorize_payment(payment_id, approver_id=user_id)

        if json_output:
            click.echo(json.dumps({'success': success, 'payment_id': payment_id}))
        else:
            if success:
                click.echo(f"Payment {payment_id} authorized.")
            else:
                click.echo(f"Failed to authorize payment {payment_id}.", err=True)

    except ImportError as e:
        _error_exit(f'AP2 not available: {e}', json_output)


@pay.command('process')
@click.argument('payment_id')
@click.pass_context
def pay_process(ctx, payment_id):
    """Process an authorized payment."""
    json_output = ctx.obj['json_output']

    try:
        from integrations.ap2.ap2_protocol import get_payment_ledger

        ledger = get_payment_ledger()
        result = ledger.process_payment(payment_id)

        if json_output:
            click.echo(json.dumps(result, indent=2, default=str))
        else:
            if result.get('success'):
                click.echo(f"Payment {payment_id} processed.")
            else:
                click.echo(f"Failed: {result.get('error', '?')}", err=True)

    except ImportError as e:
        _error_exit(f'AP2 not available: {e}', json_output)


# ─── hart mcp — MCP server management ───


@hart.group()
@click.pass_context
def mcp(ctx):
    """Model Context Protocol: list servers, discover and execute tools."""
    pass


@mcp.command('list')
@click.pass_context
def mcp_list(ctx):
    """List MCP servers and available tools."""
    json_output = ctx.obj['json_output']

    try:
        from integrations.mcp.mcp_integration import MCPToolRegistry

        registry = MCPToolRegistry()
        registry_loaded = False
        try:
            from integrations.mcp.mcp_integration import load_user_mcp_servers
            load_user_mcp_servers()
            registry_loaded = True
        except Exception:
            pass

        tools = registry.get_tool_definitions()

        if json_output:
            click.echo(json.dumps({'tools': tools, 'loaded': registry_loaded}, indent=2, default=str))
        else:
            if not tools:
                click.echo('No MCP tools discovered. Configure mcp_servers.json to add servers.')
                return
            click.echo(f'MCP Tools ({len(tools)}):')
            for t in tools:
                name = t.get('name', '?')
                desc = t.get('description', '')[:60]
                click.echo(f"  {name:30} {desc}")

    except ImportError as e:
        _error_exit(f'MCP integration not available: {e}', json_output)


@mcp.command('execute')
@click.argument('tool_name')
@click.argument('arguments', default='{}')
@click.pass_context
def mcp_execute(ctx, tool_name, arguments):
    """Execute an MCP tool. Arguments as JSON string."""
    json_output = ctx.obj['json_output']

    try:
        args = json.loads(arguments)
    except json.JSONDecodeError:
        _error_exit(f'Invalid JSON arguments: {arguments}', json_output)
        return

    try:
        from integrations.mcp.mcp_integration import MCPToolRegistry

        registry = MCPToolRegistry()
        tool_fn = registry.create_tool_function(tool_name)
        if not tool_fn:
            _error_exit(f'MCP tool not found: {tool_name}', json_output)
            return

        result = tool_fn(**args)

        if json_output:
            click.echo(json.dumps({'result': result}, indent=2, default=str))
        else:
            click.echo(str(result))

    except ImportError as e:
        _error_exit(f'MCP integration not available: {e}', json_output)


@mcp.command('servers')
@click.pass_context
def mcp_servers(ctx):
    """Show configured MCP servers."""
    json_output = ctx.obj['json_output']

    config_path = os.path.join(os.getcwd(), 'mcp_servers.json')
    if not os.path.exists(config_path):
        if json_output:
            click.echo(json.dumps({'servers': [], 'config_file': config_path}))
        else:
            click.echo(f'No mcp_servers.json found at {config_path}')
            click.echo('Create one with: {"servers": [{"name": "...", "url": "...", "enabled": true}]}')
        return

    try:
        with open(config_path) as f:
            config = json.load(f)
        servers = config.get('servers', [])

        if json_output:
            click.echo(json.dumps(config, indent=2))
        else:
            click.echo(f'MCP Servers ({len(servers)}):')
            for s in servers:
                enabled = 'ON' if s.get('enabled', True) else 'OFF'
                click.echo(f"  [{enabled}] {s.get('name', '?'):20} {s.get('url', '?')}")
    except Exception as e:
        _error_exit(f'Error reading MCP config: {e}', json_output)


# ─── hart compute — Compute management ───


@hart.group()
@click.pass_context
def compute(ctx):
    """Compute policy, resource monitoring, peer network."""
    pass


@compute.command('status')
@click.pass_context
def compute_status(ctx):
    """Show current compute policy and resource status."""
    server = ctx.obj['server']
    json_output = ctx.obj['json_output']

    result = _api_get(f'{server}/api/settings/compute', json_output)
    if not json_output and result:
        click.echo('Compute Policy:')
        for key, val in result.items():
            if isinstance(val, (dict, list)):
                click.echo(f"  {key}: {json.dumps(val, default=str)}")
            else:
                click.echo(f"  {key}: {val}")


@compute.command('providers')
@click.pass_context
def compute_providers(ctx):
    """List available compute providers and models."""
    server = ctx.obj['server']
    json_output = ctx.obj['json_output']

    result = _api_get(f'{server}/api/settings/compute/provider', json_output)
    if not json_output and result:
        click.echo('Available Providers:')
        for provider in result.get('available_providers', []):
            click.echo(f"  - {provider}")
        local = result.get('local_models', {})
        if local:
            click.echo(f"\nLocal Models: {json.dumps(local, default=str)}")
        cloud = result.get('cloud_models', {})
        if cloud:
            click.echo(f"Cloud Models: {json.dumps(cloud, default=str)}")


@compute.command('pressure')
@click.pass_context
def compute_pressure(ctx):
    """Show system resource pressure (CPU, memory, GPU)."""
    server = ctx.obj['server']
    json_output = ctx.obj['json_output']

    result = _api_get(f'{server}/api/system/pressure', json_output)
    if not json_output and result:
        click.echo('System Pressure:')
        for key, val in result.items():
            click.echo(f"  {key}: {val}")


@compute.command('join')
@click.option('--capacity', default=1.0, type=float, help='Compute capacity to contribute')
@click.option('--electricity-rate', default=0.0, type=float, help='Electricity rate $/kWh')
@click.pass_context
def compute_join(ctx, capacity, electricity_rate):
    """Join the peer-to-peer compute network."""
    server = ctx.obj['server']
    user_id = ctx.obj['user_id']
    json_output = ctx.obj['json_output']

    payload = {
        'node_id': user_id,
        'compute_capacity': capacity,
        'electricity_rate_kwh': electricity_rate,
    }
    result = _api_post(f'{server}/api/settings/compute/provider/join', payload, json_output)
    if not json_output and result:
        click.echo(f"Joined compute network as {user_id}")


@compute.command('revenue')
@click.pass_context
def compute_revenue(ctx):
    """Show revenue dashboard (90/9/1 split)."""
    server = ctx.obj['server']
    json_output = ctx.obj['json_output']

    result = _api_get(f'{server}/api/revenue/dashboard', json_output)
    if not json_output and result:
        click.echo('Revenue Dashboard:')
        for key, val in result.items():
            if isinstance(val, dict):
                click.echo(f"  {key}:")
                for k2, v2 in val.items():
                    click.echo(f"    {k2}: {v2}")
            else:
                click.echo(f"  {key}: {val}")


# ─── hart vision — Visual agent ───


@hart.command()
@click.argument('task_description')
@click.option('--mode', default='auto',
              type=click.Choice(['auto', 'full', 'lite', 'headless']))
@click.pass_context
def vision(ctx, task_description, mode):
    """Execute a visual agent task (VLM pipeline)."""
    server = ctx.obj['server']
    user_id = ctx.obj['user_id']
    json_output = ctx.obj['json_output']

    payload = {
        'task_description': task_description,
        'user_id': user_id,
        'prompt_id': '0',
    }
    result = _api_post(f'{server}/visual_agent', payload, json_output)
    if not json_output and result:
        output = result.get('response', result.get('output', result.get('message', '')))
        if output:
            click.echo(output)
        else:
            click.echo(json.dumps(result, indent=2, default=str))


# ─── hart voice — Audio / transcription ───


@hart.group()
@click.pass_context
def voice(ctx):
    """Voice and audio operations."""
    pass


@voice.command('transcribe')
@click.argument('audio_file', type=click.Path(exists=True))
@click.option('--language', default='en', help='Language code')
@click.pass_context
def voice_transcribe(ctx, audio_file, language):
    """Transcribe an audio file to text."""
    server = ctx.obj['server']
    json_output = ctx.obj['json_output']

    import requests

    try:
        with open(audio_file, 'rb') as f:
            files = {'audio_file': (os.path.basename(audio_file), f)}
            data = {'language': language}
            resp = requests.post(
                f'{server}/api/voice/transcribe',
                files=files, data=data, timeout=120,
            )
            result = resp.json()

        if json_output:
            click.echo(json.dumps(result, indent=2, default=str))
        else:
            text = result.get('text', result.get('transcription', ''))
            if text:
                click.echo(text)
            else:
                click.echo(json.dumps(result, indent=2, default=str))

    except requests.ConnectionError:
        _error_exit(f'Cannot connect to server at {server}', json_output)
    except Exception as e:
        _error_exit(str(e), json_output)


# ─── hart a2a — Agent-to-Agent protocol ───


@hart.group()
@click.pass_context
def a2a(ctx):
    """Google A2A protocol: discover and message agents."""
    pass


@a2a.command('discover')
@click.argument('agent_url')
@click.pass_context
def a2a_discover(ctx, agent_url):
    """Discover an agent via its .well-known/agent.json."""
    json_output = ctx.obj['json_output']
    import requests

    url = agent_url.rstrip('/')
    if not url.endswith('.well-known/agent.json'):
        url = f'{url}/.well-known/agent.json'

    try:
        resp = requests.get(url, timeout=15)
        card = resp.json()

        if json_output:
            click.echo(json.dumps(card, indent=2, default=str))
        else:
            click.echo(f"Agent: {card.get('name', '?')}")
            click.echo(f"Description: {card.get('description', '?')}")
            click.echo(f"URL: {card.get('url', '?')}")
            click.echo(f"Version: {card.get('version', '?')}")
            skills = card.get('skills', [])
            if skills:
                click.echo('Skills:')
                for s in skills:
                    click.echo(f"  - {s.get('name', s.get('id', '?'))}: {s.get('description', '')[:60]}")

    except requests.ConnectionError:
        _error_exit(f'Cannot connect to {url}', json_output)
    except Exception as e:
        _error_exit(str(e), json_output)


@a2a.command('send')
@click.argument('agent_url')
@click.argument('message')
@click.pass_context
def a2a_send(ctx, agent_url, message):
    """Send a task to a remote A2A agent."""
    json_output = ctx.obj['json_output']
    import requests

    url = agent_url.rstrip('/')
    payload = {
        'jsonrpc': '2.0',
        'method': 'message/send',
        'params': {
            'message': {
                'role': 'user',
                'parts': [{'type': 'text', 'text': message}],
            }
        },
        'id': f'hart-cli-{os.getpid()}',
    }

    try:
        resp = requests.post(f'{url}/jsonrpc', json=payload, timeout=120)
        result = resp.json()

        if json_output:
            click.echo(json.dumps(result, indent=2, default=str))
        else:
            r = result.get('result', result)
            state = r.get('state', '?')
            click.echo(f"Task state: {state}")
            artifacts = r.get('artifacts', [])
            for a in artifacts:
                for part in a.get('parts', []):
                    if part.get('type') == 'text':
                        click.echo(part['text'])

    except requests.ConnectionError:
        _error_exit(f'Cannot connect to {agent_url}', json_output)
    except Exception as e:
        _error_exit(str(e), json_output)


@a2a.command('agents')
@click.pass_context
def a2a_agents(ctx):
    """List locally registered A2A agents."""
    server = ctx.obj['server']
    json_output = ctx.obj['json_output']

    # A2A agents are auto-discovered from prompts/ directory
    import glob as glob_mod
    recipe_files = sorted(glob_mod.glob('prompts/*_recipe.json'))

    agents = []
    for f in recipe_files:
        basename = os.path.basename(f).replace('_recipe.json', '')
        parts = basename.split('_')
        if len(parts) >= 2:
            agents.append({
                'agent_id': basename,
                'prompt_id': parts[0],
                'flow_id': parts[1],
                'card_url': f'{server}/a2a/{basename}/.well-known/agent.json',
            })

    if json_output:
        click.echo(json.dumps(agents, indent=2))
    else:
        if not agents:
            click.echo('No A2A agents found (no recipes in prompts/).')
            return
        click.echo(f'A2A Agents ({len(agents)}):')
        for a in agents:
            click.echo(f"  {a['agent_id']:30} {a['card_url']}")


# ─── hart skill — Skill management ───


@hart.group()
@click.pass_context
def skill(ctx):
    """Agent skills: list, discover, ingest."""
    pass


@skill.command('list')
@click.pass_context
def skill_list(ctx):
    """List registered agent skills."""
    server = ctx.obj['server']
    json_output = ctx.obj['json_output']

    result = _api_get(f'{server}/api/skills/list', json_output)
    if not json_output and result:
        skills = result if isinstance(result, dict) else {}
        total = sum(len(v.get('skills', [])) if isinstance(v, dict) else 0
                    for v in skills.values())
        click.echo(f'Skills ({total} across {len(skills)} agents):')
        for agent_id, data in skills.items():
            agent_skills = data.get('skills', []) if isinstance(data, dict) else []
            if agent_skills:
                click.echo(f"  {agent_id}:")
                for s in agent_skills:
                    name = s if isinstance(s, str) else s.get('name', '?')
                    click.echo(f"    - {name}")


@skill.command('ingest')
@click.argument('skill_file', type=click.Path(exists=True))
@click.option('--agent-id', required=True, help='Agent to assign skill to')
@click.pass_context
def skill_ingest(ctx, skill_file, agent_id):
    """Ingest a skill definition from a file."""
    server = ctx.obj['server']
    json_output = ctx.obj['json_output']

    with open(skill_file) as f:
        content = f.read()

    payload = {
        'agent_id': agent_id,
        'skill_content': content,
    }
    result = _api_post(f'{server}/api/skills/ingest', payload, json_output)
    if not json_output and result:
        click.echo(f"Skill ingested for agent {agent_id}")


# ─── hart schedule — Scheduled tasks ───


@hart.command('schedule')
@click.argument('prompt')
@click.option('--cron', default='', help='Cron expression (e.g. "0 9 * * *")')
@click.option('--interval', default=0, type=int, help='Interval in minutes')
@click.pass_context
def schedule(ctx, prompt, cron, interval):
    """Schedule a recurring agent task."""
    server = ctx.obj['server']
    user_id = ctx.obj['user_id']
    json_output = ctx.obj['json_output']

    payload = {
        'user_id': user_id,
        'prompt_id': '0',
        'prompt': prompt,
    }
    if cron:
        payload['cron'] = cron
    if interval:
        payload['interval_minutes'] = interval

    result = _api_post(f'{server}/time_agent', payload, json_output)
    if not json_output and result:
        output = result.get('response', result.get('output', result.get('message', '')))
        if output:
            click.echo(output)
        else:
            click.echo(json.dumps(result, indent=2, default=str))


# ─── hart zeroshot — Zero-shot inference ───


@hart.command('zeroshot')
@click.argument('task')
@click.option('--model', 'model_override', default='', help='Model override')
@click.pass_context
def zeroshot(ctx, task, model_override):
    """Execute a zero-shot task without a saved recipe."""
    server = ctx.obj['server']
    user_id = ctx.obj['user_id']
    json_output = ctx.obj['json_output']
    model = model_override or ctx.obj['model']

    payload = {
        'user_id': user_id,
        'prompt': task,
    }
    if model:
        payload['model'] = model

    result = _api_post(f'{server}/zeroshot/', payload, json_output)
    if not json_output and result:
        output = result.get('response', result.get('output', result.get('message', '')))
        if output:
            click.echo(output)
        else:
            click.echo(json.dumps(result, indent=2, default=str))


# ─── Utilities ───


def _api_get(url, json_output=False, silent=False):
    """GET helper — returns parsed JSON or None."""
    import requests
    try:
        resp = requests.get(url, timeout=30)
        data = resp.json()
        if json_output:
            click.echo(json.dumps(data, indent=2, default=str))
        return data
    except requests.ConnectionError:
        if not silent:
            _error_exit(f'Cannot connect to {url.split("/api")[0]}', json_output)
        return None
    except Exception as e:
        if not silent:
            _error_exit(str(e), json_output)
        return None


def _api_post(url, payload, json_output=False):
    """POST helper — returns parsed JSON or None."""
    import requests
    try:
        resp = requests.post(url, json=payload, timeout=120)
        data = resp.json()
        if json_output:
            click.echo(json.dumps(data, indent=2, default=str))
        return data
    except requests.ConnectionError:
        _error_exit(f'Cannot connect to {url.split("/api")[0]}', json_output)
        return None
    except Exception as e:
        _error_exit(str(e), json_output)
        return None


def _error_exit(message, json_output=False):
    """Print error and exit."""
    if json_output:
        click.echo(json.dumps({'error': message}))
    else:
        click.echo(f'Error: {message}', err=True)
    sys.exit(1)


if __name__ == '__main__':
    hart()
