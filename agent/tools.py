THINKING_FIELDS = {
    'observations': {
        'type': 'array',
        'items': {'type': 'string'},
        'description': 'Each distinct thing you can see now, with where it is in the image.'
    },
    'target': {
        'type': 'string',
        'description': 'Which thing you are going for and why, or none yet.'
    },
    'confidence': {
        'type': 'number',
        'description': '0 to 1: how sure you are that the target is the right one.'
    }
}

def make_tool(name: str, description: str, props: dict = None, required: list = None):
    all_props = {**(props or {}), **THINKING_FIELDS}
    all_required = (required or []) + ['observations', 'target', 'confidence']
    return {
        'name': name,
        'description': description,
        'input_schema': {
            'type': 'object',
            'properties': all_props,
            'required': all_required
        }
    }

TOOLS = [
    make_tool(
        'move',
        'Drive forward (positive) or backward (negative) by a distance in metres. '
        'Stops early if the robot hits something. Returns how far it moved, '
        'whether it collided, and a new camera frame.',
        {'distance': {'type': 'number', 'description': 'Distance to move in metres'}},
        ['distance']
    ),
    make_tool(
        'turn',
        'Turn in place by degrees. Positive turns right (clockwise), negative turns left. '
        'Returns the new heading and a new camera frame.',
        {'degrees': {'type': 'number', 'description': 'Angle to rotate in degrees'}},
        ['degrees']
    ),
    make_tool(
        'pickup',
        'Try to pick up the nearest object within reach straight ahead. '
        'Returns only whether it worked.'
    ),
    make_tool(
        'drop',
        'Release the held object. Returns whether it landed in the drop zone.'
    ),
    make_tool(
        'read_depth',
        'Read the distance in metres to the nearest thing straight ahead. '
        'It does not say what the thing is.'
    ),
    make_tool(
        'done',
        'End the run when the object has been delivered or you cannot continue.'
    ),
]

# Zero-leak compliant system prompt (describes only robot kinematics, camera and tools)
SYSTEM_PROMPT = """You control a wheeled robot with one forward-facing camera, 90 degrees wide, tilted slightly down. You only know the world through the camera frames, one depth reading and a collision flag.
Units are metres and degrees. Positive turns go right. Forward is the direction the camera faces.
Call exactly one tool per step. Fill in observations, target and confidence every time.
Commands may be indirect or need reasoning. Work out what the person means from what you see.
Find things by turning and looking. If nothing matches after a full sweep, drive toward open space and sweep again.
Centre the target in view, read the depth, move part of the way, then look again. Confirm the target at close range before pickup.
If you collide, back off, pick a different heading and try again. Stay direct: do not wander once the target is confirmed.
Finish with done once the object has been dropped, or if you cannot continue."""

