import anthropic
import json
from dotenv import load_dotenv
import os
import copy
import requests
import getpass
import datetime

load_dotenv()


client = anthropic.Anthropic()
current_user = getpass.getuser()


current_workflow = {}
staged_plan = {}
submitted_ids = []
comfyui_url = os.getenv("COMFYUI_URL", "http://127.0.0.1:8188")
current_output_dir = ""

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
        "description": "Looks through the current ComfyUI workflow and returns the current value stored in the workflow. Call this if user asks for a value in the workflow.",
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
        "description": "Stages the batch render parameters for user review. Call this when the user is trying to make changes to the workflow for each of the renders that are going to be sent. Does NOT write anything to disk — just shows the user a summary of what will be rendered for confirmation.",
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
        "name": "submit_jobs",
        "description": "Submits all batched .json render files in the output directory to ComfyUI as jobs. Only call this after write_batch has completed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "output_dir": {
                    "type": "string",
                    "description": "Directory containing the batched .json render files to submit"
                }
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
    }
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

def get_current_parameter(parameter_names: list) -> dict:
    results = {}
    for node_id, node in current_workflow.items():
        for param, value in node["inputs"].items():
            if param in parameter_names and not isinstance(value, list):
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
            response = requests.post(f"{comfyui_url}/prompt", json=payload)
            response.raise_for_status()
            prompt_id = response.json().get("prompt_id")
            submitted_ids.append(prompt_id)
            results.append(f"✅ {filename} → queued as {prompt_id}")
        except requests.exceptions.ConnectionError:
            return f"❌ Could not connect to ComfyUI at {comfyui_url}. Is it running?"
        except requests.exceptions.HTTPError as e:
            results.append(f"❌ {filename} → failed: {e}")

    return "\n".join(results)


def download_outputs() -> str:
    if not submitted_ids:
        return "No submitted jobs found. Please submit jobs first."
    
    try:
        history_response = requests.get(f"{comfyui_url}/history")
        history_response.raise_for_status()
        history = history_response.json()
    except requests.exceptions.ConnectionError:
        return f"❌ Could not connect to ComfyUI at {comfyui_url}. Is it running?"

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
                        file_response = requests.get(f"{comfyui_url}/view", params=params)
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
        queue_response = requests.get(f"{comfyui_url}/queue")
        queue_response.raise_for_status()
        queue = queue_response.json()

        history_response = requests.get(f"{comfyui_url}/history")
        history_response.raise_for_status()
        history = history_response.json()
    except requests.exceptions.ConnectionError:
        return f"❌ Could not connect to ComfyUI at {comfyui_url}. Is it running?"

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
            response = requests.post(f"{comfyui_url}/interrupt")
            response.raise_for_status()
            results.append("🛑 Interrupted currently running job.")

        # Clear all pending jobs from queue
        if cancel_all:
            response = requests.post(f"{comfyui_url}/queue", json={"clear": True})
            response.raise_for_status()
            results.append("🛑 Cleared all pending jobs from queue.")

        # Cancel specific jobs by ID
        elif prompt_ids:
            response = requests.post(f"{comfyui_url}/queue", json={"delete": prompt_ids})
            response.raise_for_status()
            results.append(f"🛑 Cancelled {len(prompt_ids)} jobs: {', '.join(prompt_ids)}")

        # Default — cancel only current batch's pending jobs
        elif not cancel_running and not cancel_all:
            response = requests.post(f"{comfyui_url}/queue", json={"delete": submitted_ids})
            response.raise_for_status()
            results.append(f"🛑 Cancelled current batch ({len(submitted_ids)} pending jobs)")

        return "\n".join(results)

    except requests.exceptions.ConnectionError:
        return f"❌ Could not connect to ComfyUI at {comfyui_url}. Is it running?"
    except requests.exceptions.HTTPError as e:
        return f"❌ Failed to cancel jobs: {e}"

tool_dispatch = {
    "parse_workflow": load_json,
    "print_workflow": print_workflow,
    "get_current_parameter": get_current_parameter,
    "stage_batch": stage_batch,
    "write_batch": write_batch,
    "submit_jobs": submit_jobs,
    "download_outputs": download_outputs,
    "cancel_jobs": cancel_jobs,
    "get_job_status": get_job_status
}

# 3. The agent loop
def run(user_message):
    messages = [{"role": "user", "content": user_message}]
    end_condition = False

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
            user_input = input("You: ")
            messages.append({"role": "user", "content": user_input})

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = tool_dispatch[block.name](**block.input)

                    print(f"Tool called: {block.name}({block.input}) → {result}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result)
                    })

            messages.append({"role": "user", "content": tool_results})



user_input = input("You: ")
run(user_input)