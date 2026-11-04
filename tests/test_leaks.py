import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import re
import glob
from agent.tools import SYSTEM_PROMPT, TOOLS

FORBIDDEN_SCENE_WORDS = [
    # colors
    r'\bred\b', r'\bblue\b', r'\byellow\b', r'\borange\b', r'\bgreen\b',
    # shapes
    r'\bcube\b', r'\bsphere\b', r'\bcylinder\b', r'\bcone\b',
    # room layout hints
    r'\bcenter\b', r'\bcorner\b', r'\bdrop zone coordinates\b',
    # counts
    r'\bsix objects\b', r'\b6 objects\b', r'\bfour shapes\b'
]

def test_system_prompt_has_no_scene_leaks():
    lower_prompt = SYSTEM_PROMPT.lower()
    for word_regex in FORBIDDEN_SCENE_WORDS:
        match = re.search(word_regex, lower_prompt)
        assert match is None, f"Leak found in SYSTEM_PROMPT: matched '{match.group(0)}'"

def test_tools_descriptions_have_no_scene_leaks():
    for t in TOOLS:
        desc = t['description'].lower()
        for word_regex in FORBIDDEN_SCENE_WORDS:
            match = re.search(word_regex, desc)
            assert match is None, f"Leak found in tool '{t['name']}' description: '{match.group(0)}'"

        # Check properties schema descriptions
        props = t.get('input_schema', {}).get('properties', {})
        for prop_name, prop_val in props.items():
            pdesc = prop_val.get('description', '').lower()
            for word_regex in FORBIDDEN_SCENE_WORDS:
                match = re.search(word_regex, pdesc)
                assert match is None, f"Leak found in tool '{t['name']}' property '{prop_name}': '{match.group(0)}'"

def test_file_naming_conventions():
    # Verify logger only produces step_XXX.png and timestamp directories
    valid_step_pattern = re.compile(r'^step_\d{3}\.png$')
    timestamp_folder_pattern = re.compile(r'^\d{8}T\d{6}Z$')

    logs_dirs = glob.glob('logs/*')
    for d in logs_dirs:
        folder_name = os.path.basename(d)
        assert timestamp_folder_pattern.match(folder_name), f"Invalid log directory name: {folder_name}"

        frames = glob.glob(os.path.join(d, '*.png'))
        for f in frames:
            fname = os.path.basename(f)
            assert valid_step_pattern.match(fname), f"Invalid frame filename leak: {fname}"
