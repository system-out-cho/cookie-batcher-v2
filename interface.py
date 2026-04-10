import anthropic
import json
from dotenv import load_dotenv
import os
import copy
import requests
import getpass
import datetime
import platform

load_dotenv()


client = anthropic.Anthropic()
current_user = getpass.getuser()


current_workflow = {}
staged_plan = {}
submitted_ids = []
comfy_server = ""
current_output_dir = ""
end_condition = False

SESSION_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "baker_sessions.json")

# 1. Define the tool
tools = [
    {
        "name": "parse_workflow",
        "description": "Parses the ComfyUI workflow JSON and returns an editable summary. Always call this first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "json_path": {
                    "type": "string",
                    "description": "Path to the ComfyUI API workflow .json file"
                }
            },
            "required": ["json_path"]
        }
    },
    {
        "name": "print_workflow",
        "description": "Prints the workflow in a user readable manner and shows all editable parameters. Call this if the user asks to see a list of parameters or Node IDs.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "get_current_parameter",
        "description": "Looks up current values by parameter name (e.g. 'cfg', 'seed') OR by node ID (e.g. '68:190'). Accepts either. Call this if user asks for a value in the workflow.",
        "input_schema": {
            "type": "object",
            "properties": {
                "parameter_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of parameter names to look up e.g. ['cfg', 'seed']"
                }
            },
            "required": ["parameter_names"]
        }
    },
    {
        "name": "stage_batch",
        "description": "Stages the batch render parameters for user review. Call this when the user is trying to make changes to the workflow for each of the renders that are going to be sent. Does NOT write anything to disk — just shows the user a summary of what will be rendered for confirmation. Node IDs may contain colons for subgraph nodes (e.g. '68:190'). Always use the full ID exactly as shown in the workflow, including any colons.",
        "input_schema": {
            "type": "object",
            "properties": {
                "render_count": {
                    "type": "integer",
                    "description": "Number of renders to generate"
                },
                "overrides": {
                    "type": "object",
                    "description": """node_id → param → list of values, one per render. 
                    Must be a nested object, NOT a string. 
                    Example: {"8": {"text": ["prompt 1", "prompt 2", "prompt 3"]}}""",
                    "additionalProperties": {
                        "type": "object",
                        "additionalProperties": {
                            "type": "array",
                            "items": {}
                        }
                    }
                },
            },
            "required": ["render_count", "overrides"]
        }
    },
    {
        "name": "write_batch",
        "description": "Writes the finalized batch render .json files to disk. Only call this after the user has explicitly confirmed the staged batch looks correct. Ask the user for a output directory, output filename, and which output nodes to write to before calling this.",
        "input_schema": {
            "type": "object",
            "properties": {
                "output_dir": {
                    "type": "string",
                    "description": "Directory path to write the .json render files to"
                },
                "output_filename": {
                    "type": "string",
                    "description": "Name for the output files e.g. 'my_render'"
                },
                "output_nodes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of node IDs to update filename_prefix on e.g. ['19', '27']. Ask the user which output nodes to write to if there are multiple."
                }
            },
            "required": ["output_dir", "output_filename", "output_nodes"]
        }
    },
    {
        "name": "set_machine",
        "description": "Sets current machine to render on, check the status of, or cancel jobs on. You must ask this before asking to submit the jobs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "machine_name": {
                    "type":"string",
                    "description": "Name of machine to render on. Available options are: kokoro, wopr, muthur, marvin, or local. Spelling is extremely important, auto-correct user and ensure the input matches one of the available options."
                }
            },
            "required": ["machine_name"]
        }
    },
    {
        "name": "submit_jobs",
        "description": "Submits all batched .json render files in the output directory to ComfyUI as jobs. Only call this after write_batch has completed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "output_dir": {
                    "type": "string",
                    "description": "Directory containing the batched .json render files to submit"
                },
            },
            "required": ["output_dir"]
        }
    },
    {
        "name": "download_outputs",
        "description": "Downloads completed render outputs from ComfyUI for the current batch. Call this after the user confirms their jobs have finished rendering.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "get_job_status",
        "description": "Checks job status in ComfyUI. By default checks only the current batch. Set all_jobs to true to see everything running on the machine.",
        "input_schema": {
            "type": "object",
            "properties": {
                "all_jobs": {
                    "type": "boolean",
                    "description": "If true, returns status of all jobs on the machine not just the current batch"
                }
            }
        }
    },
    {
        "name": "cancel_jobs",
        "description": "Cancels jobs in ComfyUI. Can cancel the current batch, specific jobs, the currently running job, or everything. Ask the user which they want before calling.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cancel_all": {
                    "type": "boolean",
                    "description": "If true, interrupts the running job AND clears all pending jobs"
                },
                "cancel_running": {
                    "type": "boolean",
                    "description": "If true, interrupts only the currently running job without touching the queue"
                },
                "prompt_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Specific prompt IDs to cancel from the queue. If omitted, cancels the current batch."
                }
            }
        }
    },
    {
    "name": "list_past_sessions",
    "description": "Lists all past render sessions from previous runs of the tool. Call this when the user wants to download past renders.",
    "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "download_past_session",
        "description": "Downloads outputs from a past session by its index from list_past_sessions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_index": {
                    "type": "integer",
                    "description": "Index of the session from list_past_sessions"
                },
                "output_dir": {
                    "type": "string",
                    "description": "Optional override for where to save the downloaded files. Defaults to the original session output dir."
                }
            },
            "required": ["session_index"]
        }
    },
    {
        "name": "set_end_condition",
        "description": "Ends the conversation with claude. Only run if user says words like `end conversation`",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "rename_outputs",
        "description": "Renames rendered image or video files to match their source .json filenames, stripping ComfyUI's extra suffixes like _00001_. Pairs each image to its json first, then renames.",
        "input_schema": {
            "type": "object",
            "properties": {
                "output_dir": {
                    "type": "string",
                    "description": "Directory to scan. Defaults to the current output directory if omitted."
                }
            }
        }
    },
]

# I'm printing these to show the user editable parameters instead of appending to save on token usage.
# If issue like "can't find what prompt is, then ask for "text" for node 6 or node 7. 
def load_json(json_path):
    global current_workflow
    with open(json_path) as f:
        current_workflow = json.load(f)
    
    #make this into callable function
    print_workflow()
    return f"Workflow loaded from {json_path} with {len(current_workflow)} nodes."  # ← tiny string, not the whole JSON

def print_workflow() -> str:
    if not current_workflow:
        return "No workflow loaded."
    
    print("\n📋 Current Workflow:")
    print("─" * 50)
    for node_id, node in current_workflow.items():
        editable = {k: v for k, v in node["inputs"].items() if not isinstance(v, list)}
        if editable:
            print(f"\n  [{node_id}] {node['_meta']['title']}")
            for param, value in editable.items():
                # Truncate long strings like prompts
                display_value = f'"{value[:80]}..."' if isinstance(value, str) and len(value) > 80 else value
                print(f"       {param}: {display_value}")
    print()
    print("─" * 50)
    print("💡 Note: Values above are NOT in Claude's context to save tokens.")
    print("   Reference params by node ID e.g. 'change text in node 6'\n")
    
    return "Workflow printed to terminal."

def set_machine(machine_name: str):
    global comfy_server

    if (machine_name == "local"):
        comfy_server = os.getenv("COMFYUI_URL")
    else:
        comfy_server = f"http://{machine_name.lower()}.fas.fa.disney.com:8188"
    
    return f"Current machine set to {machine_name}."


def get_current_parameter(parameter_names: list) -> dict:
    results = {}
    for node_id, node in current_workflow.items():
        for param, value in node["inputs"].items():
            if not isinstance(value, list):
                # Match by param name OR by node_id
                if param in parameter_names or node_id in parameter_names:
                    if node_id not in results:
                        results[node_id] = {
                            "title": node["_meta"]["title"],
                            "class_type": node["class_type"],
                            "params": {}
                        }
                    results[node_id]["params"][param] = value
    return results

def stage_batch(render_count: int, overrides: dict) -> str:
    staged_plan.update({
        "render_count": render_count,
        "overrides": overrides,
    })
    
    # Build a readable summary for Claude to show the user
    lines = []
    for i in range(render_count):
        render_changes = {}
        for node_id, params in overrides.items():
            for param, values in params.items():
                render_changes[param] = values[i]
        lines.append(f"render_{i+1:03d}: {render_changes}")
    
    return "\n".join(lines)

def write_batch(output_filename: str, output_nodes: list, output_dir: str) -> str:
    if not staged_plan:
        return "No staged batch found. Please stage a batch first."
    if not current_workflow:
        return "No workflow loaded. Please parse a workflow first."

    render_count = staged_plan["render_count"]
    overrides = staged_plan["overrides"]

    global current_output_dir
    current_output_dir = output_dir 

    os.makedirs(output_dir, exist_ok=True)

    for i in range(render_count):
        render = copy.deepcopy(current_workflow)

        for node_id, params in overrides.items():
            for param, values in params.items():
                render[node_id]["inputs"][param] = values[i]
        
        output_path = f"{output_dir}/{output_filename}_{i+1:03d}.json"
        
        for node_id in output_nodes:
            if node_id in render:
                render[node_id]["inputs"]["filename_prefix"] = f"{output_filename}_{i+1:03d}"

        with open(output_path, "w") as f:
            json.dump(render, f, indent=2)

    return f"Written {render_count} render files to {output_dir}"

def submit_jobs(output_dir: str) -> str:
    global submitted_ids
    submitted_ids = []

    if (not comfy_server):
        return "Please set a machine to render on before submitting jobs."

    json_files = sorted([
        f for f in os.listdir(output_dir) if f.endswith(".json")
    ])

    if not json_files:
        return f"No .json files found in {output_dir}"

    submitted_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    results = []
    for i, filename in enumerate(json_files):
        filepath = os.path.join(output_dir, filename)

        with open(filepath) as f:
            workflow = json.load(f)

        payload = {
            "prompt": workflow,
            "extra_data": {
                "submitted_by": current_user,
                "submitted_at": submitted_at,
                "batch_file": filename,
                "batch_index": i + 1,
                "batch_total": len(json_files),
                "output_dir": output_dir
            }
        }

        try:
            response = requests.post(f"{comfy_server}/prompt", json=payload)
            response.raise_for_status()
            prompt_id = response.json().get("prompt_id")
            submitted_ids.append(prompt_id)
            results.append(f"✅ {filename} → queued as {prompt_id}")
        except requests.exceptions.ConnectionError:
            return f"❌ Could not connect to ComfyUI at {comfy_server}. Is it running?"
        except requests.exceptions.HTTPError as e:
            results.append(f"❌ {filename} → failed: {e}")
    save_session(output_dir)
    return "\n".join(results)


def download_outputs() -> str:
    if not submitted_ids:
        return "No submitted jobs found. Please submit jobs first."
    
    try:
        history_response = requests.get(f"{comfy_server}/history")
        history_response.raise_for_status()
        history = history_response.json()
    except requests.exceptions.ConnectionError:
        return f"❌ Could not connect to ComfyUI at {comfy_server}. Is it running?"

    os.makedirs(current_output_dir, exist_ok=True)

    results = []
    for prompt_id in submitted_ids:
        if prompt_id not in history:
            results.append(f"⏳ {prompt_id} not finished yet")
            continue

        prompt_data = history[prompt_id].get("prompt", [])
        extra = prompt_data[3] if len(prompt_data) > 3 else {}

        outputs = history[prompt_id].get("outputs", {})
        
        for node_id, node_output in outputs.items():
            for file_type_key in ["images", "gifs"]:
                for file in node_output.get(file_type_key, []):
                    filename = file["filename"]
                    params = {
                        "filename": filename,
                        "type": file.get("type", "output"),
                        "subfolder": file.get("subfolder", "")
                    }
                    try:
                        file_response = requests.get(f"{comfy_server}/view", params=params)
                        file_response.raise_for_status()
                        save_path = os.path.join(current_output_dir, filename)
                        with open(save_path, "wb") as f:
                            f.write(file_response.content)
                        results.append(f"✅ Downloaded {filename}")
                    except Exception as e:
                        results.append(f"❌ Failed to download {filename}: {e}")

    return "\n".join(results) if results else "No outputs found for current batch."

def get_job_status(all_jobs: bool = False) -> str:
    if not all_jobs and not submitted_ids:
        return "No submitted jobs found for current batch."
    
    try:
        queue_response = requests.get(f"{comfy_server}/queue")
        queue_response.raise_for_status()
        queue = queue_response.json()

        history_response = requests.get(f"{comfy_server}/history")
        history_response.raise_for_status()
        history = history_response.json()
    except requests.exceptions.ConnectionError:
        return f"❌ Could not connect to ComfyUI at {comfy_server}. Is it running?"

    running = [job[1] for job in queue.get("queue_running", [])]
    pending = [job[1] for job in queue.get("queue_pending", [])]

    # Either check all jobs on machine or just current batch
    if all_jobs:
        ids_to_check = list(set(running + pending + list(history.keys())))
    else:
        ids_to_check = submitted_ids

    results = []
    for prompt_id in ids_to_check:
        extra = {}
        if prompt_id in history:
            prompt_data = history[prompt_id].get("prompt", [])
            extra = prompt_data[3] if len(prompt_data) > 3 else {}
        else:
            for job in queue.get("queue_running", []) + queue.get("queue_pending", []):
                if job[1] == prompt_id:
                    extra = job[3] if len(job) > 3 else {}
                    break

        meta = ""
        if extra:
            meta = (
                f"\n       👤 {extra.get('submitted_by', 'unknown')}"
                f"\n       🕐 {extra.get('submitted_at', 'unknown')}"
                f"\n       📄 {extra.get('batch_file', 'unknown')} "
                f"({extra.get('batch_index', '?')}/{extra.get('batch_total', '?')})"
                f"\n       📁 {extra.get('output_dir', 'unknown')}"
            )

        if prompt_id in running:
            results.append(f"🔄 {prompt_id} → currently rendering{meta}")
        elif prompt_id in pending:
            position = pending.index(prompt_id) + 1
            results.append(f"⏳ {prompt_id} → queued (position {position}){meta}")
        elif prompt_id in history:
            results.append(f"✅ {prompt_id} → completed{meta}")
        else:
            results.append(f"❓ {prompt_id} → unknown{meta}")

    return "\n".join(results) if results else "No jobs found on machine."


def cancel_jobs(cancel_all: bool = False, cancel_running: bool = False, prompt_ids: list = None) -> str:
    try:
        results = []

        # Interrupt the currently running job
        if cancel_running or cancel_all:
            response = requests.post(f"{comfy_server}/interrupt")
            response.raise_for_status()
            results.append("🛑 Interrupted currently running job.")

        # Clear all pending jobs from queue
        if cancel_all:
            response = requests.post(f"{comfy_server}/queue", json={"clear": True})
            response.raise_for_status()
            results.append("🛑 Cleared all pending jobs from queue.")

        # Cancel specific jobs by ID
        elif prompt_ids:
            response = requests.post(f"{comfy_server}/queue", json={"delete": prompt_ids})
            response.raise_for_status()
            results.append(f"🛑 Cancelled {len(prompt_ids)} jobs: {', '.join(prompt_ids)}")

        # Default — cancel only current batch's pending jobs
        elif not cancel_running and not cancel_all:
            response = requests.post(f"{comfy_server}/queue", json={"delete": submitted_ids})
            response.raise_for_status()
            results.append(f"🛑 Cancelled current batch ({len(submitted_ids)} pending jobs)")

        return "\n".join(results)

    except requests.exceptions.ConnectionError:
        return f"❌ Could not connect to ComfyUI at {comfy_server}. Is it running?"
    except requests.exceptions.HTTPError as e:
        return f"❌ Failed to cancel jobs: {e}"

def rename_outputs(output_dir: str = None) -> str:
    """Renames image files to match their source .json filenames."""
    target_dir = output_dir or current_output_dir
    if not target_dir:
        return "No output directory set."

    files = os.listdir(target_dir)
    pngs = sorted([f for f in files if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.gif', '.mp4'))])
    jsons = sorted([f for f in files if f.lower().endswith('.json')])
    json_bases = {os.path.splitext(j)[0]: j for j in jsons}

    results = []
    for png in pngs:
        png_name, png_ext = os.path.splitext(png)
        candidate = png_name.rstrip('_')
        matched_base = None

        while candidate:
            if candidate in json_bases:
                matched_base = candidate
                break
            if '_' in candidate:
                candidate = candidate.rsplit('_', 1)[0]
            else:
                break

        if matched_base:
            new_name = matched_base + png_ext
            if new_name != png:
                src = os.path.join(target_dir, png)
                dst = os.path.join(target_dir, new_name)
                os.rename(src, dst)
                results.append(f"✏️  {png} → {new_name}")
            else:
                results.append(f"✅ {png} (already clean)")
        else:
            results.append(f"❓ {png} (no matching json, skipped)")

    return "\n".join(results) if results else "No image files found."
def save_session(output_dir: str):
    sessions = load_sessions()
    entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "machine": comfy_server,
        "output_dir": output_dir,
        "prompt_ids": submitted_ids.copy()
    }
    sessions.append(entry)
    with open(SESSION_LOG, "w") as f:
        json.dump(sessions, f, indent=2)

def load_sessions() -> list:
    if not os.path.exists(SESSION_LOG):
        return []
    with open(SESSION_LOG) as f:
        return json.load(f)

def list_past_sessions() -> str:
    sessions = load_sessions()
    if not sessions:
        return "No past sessions found."
    lines = []
    for i, s in enumerate(sessions):
        lines.append(
            f"[{i}] {s['timestamp']}  |  {len(s['prompt_ids'])} jobs  "
            f"|  {s['output_dir']}  |  {s['machine']}"
        )
    return "\n".join(lines)

def download_past_session(session_index: int, output_dir: str = None) -> str:
    global submitted_ids, current_output_dir, comfy_server
    sessions = load_sessions()
    if session_index >= len(sessions):
        return "Invalid session index."
    
    session = sessions[session_index]
    submitted_ids = session["prompt_ids"]
    current_output_dir = output_dir or session["output_dir"]
    comfy_server = session["machine"]  # restore the machine too
    
    return download_outputs()  # reuse existing logic


def set_end_condition():
    global end_condition
    end_condition = True
    return "Ending conversation..."

tool_dispatch = {
    "parse_workflow": load_json,
    "print_workflow": print_workflow,
    "get_current_parameter": get_current_parameter,
    "stage_batch": stage_batch,
    "write_batch": write_batch,
    "submit_jobs": submit_jobs,
    "download_outputs": download_outputs,
    "cancel_jobs": cancel_jobs,
    "get_job_status": get_job_status,
    "set_machine": set_machine,
    "rename_outputs": rename_outputs,
    "list_past_sessions": list_past_sessions,
    "download_past_session": download_past_session,
    "set_end_condition": set_end_condition
}

# 3. The agent loop
def run(user_message):
    messages = [{"role": "user", "content": user_message}]

    while not end_condition:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            system="Be concise. After parsing a workflow just confirm it was loaded and ask what the user wants to do. Do not print tables or long summaries unless the user asks.",
            max_tokens=1024,
            tools=tools,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            print(response.content[0].text)
            user_input = input(f"{current_user} (baker): ")
            messages.append({"role": "user", "content": user_input})

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = tool_dispatch[block.name](**block.input)

                    print(f"Tool called: {block.name}({block.input}) → \n{result}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result)
                    })

            messages.append({"role": "user", "content": tool_results})



user_input = input(f"{current_user} (baker): ")
run(user_input)