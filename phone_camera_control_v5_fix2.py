bl_info = {
    "name": "Phone Camera Control v5fix2",
    "author": "Custom",
    "version": (5, 2, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Phone Cam",
    "description": "Phone gyro camera - dual timer fix",
    "category": "Camera",
}

import bpy
import threading
import json
import socket
import time
from mathutils import Quaternion, Vector

# ── WebSocket ──────────────────────────────────

def _ws_handshake(conn):
    import hashlib, base64
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk: return False
        data += chunk
    key = None
    for line in data.decode("utf-8", errors="replace").split("\r\n"):
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip(); break
    if not key: return False
    magic = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    accept = __import__("base64").b64encode(
        __import__("hashlib").sha1((key + magic).encode()).digest()).decode()
    conn.sendall((
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    ).encode())
    return True

def _ws_recv_frame(conn):
    try:
        header = b""
        while len(header) < 2:
            b = conn.recv(2 - len(header))
            if not b: return None
            header += b
        b1, b2 = header[0], header[1]
        opcode = b1 & 0x0F
        masked = (b2 & 0x80) != 0
        length = b2 & 0x7F
        if opcode == 8: return None
        if opcode not in (1, 2): return None
        if length == 126:
            raw = b""
            while len(raw) < 2: raw += conn.recv(2 - len(raw))
            length = int.from_bytes(raw, "big")
        elif length == 127:
            raw = b""
            while len(raw) < 8: raw += conn.recv(8 - len(raw))
            length = int.from_bytes(raw, "big")
        mask_key = b""
        if masked:
            while len(mask_key) < 4: mask_key += conn.recv(4 - len(mask_key))
        payload = b""
        while len(payload) < length:
            chunk = conn.recv(length - len(payload))
            if not chunk: return None
            payload += chunk
        if masked:
            payload = bytes(payload[i] ^ mask_key[i % 4] for i in range(len(payload)))
        return payload.decode("utf-8", errors="replace")
    except: return None

# ── Global state ───────────────────────────────

_server_socket  = None
_running        = False
_latest_data    = {}
_data_lock      = threading.Lock()
_client_count   = 0

_is_recording       = False
_record_fps         = 24
_record_frame       = 0
_record_start_frame = 1
_last_kf_time       = 0.0   # wall-clock time of last keyframe insert

AXIS_FIX = Quaternion((0.7071068, 0.7071068, 0.0, 0.0))

# ── Playback handler — หยุด rotation timer ตอน Play ──
_playback_was_playing = False

def _on_frame_change(scene, depsgraph):
    """ถูกเรียกทุกครั้งที่ frame เปลี่ยน (รวมถึงตอน Play)
    ใช้ตรวจว่า animation กำลัง play อยู่มั้ย
    ถ้า play → ปิด rotation timer ชั่วคราว
    ถ้า stop → เปิด rotation timer กลับ"""
    global _playback_was_playing, _rot_timer_registered
    is_playing = scene.frame_current != getattr(_on_frame_change, '_last_frame', scene.frame_current)
    _on_frame_change._last_frame = scene.frame_current

def _on_render_pre(scene):
    pass

# ใช้ screen handler แทน — ตรวจ is_animplaying
_anim_was_playing = False

def _check_playback():
    """Timer ตรวจสอบ playback state ทุก 0.1s"""
    global _anim_was_playing, _rot_timer_registered
    try:
        # ตรวจ context ว่า animation กำลัง play อยู่มั้ย
        is_playing = False
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    for space in area.spaces:
                        if hasattr(space, 'overlay'):
                            pass
        # วิธีที่ reliable ที่สุด: เช็ค is_animation_playing
        is_playing = bpy.context.screen.is_animation_playing if hasattr(bpy.context, 'screen') and bpy.context.screen else False

        if is_playing and not _anim_was_playing:
            # เริ่ม play → หยุด rotation timer
            if _rot_timer_registered and bpy.app.timers.is_registered(_timer_rotation):
                bpy.app.timers.unregister(_timer_rotation)
                _rot_timer_registered = False
            _anim_was_playing = True

        elif not is_playing and _anim_was_playing:
            # หยุด play → เริ่ม rotation timer กลับ
            if not _rot_timer_registered:
                bpy.app.timers.register(_timer_rotation, persistent=True)
                _rot_timer_registered = True
            _anim_was_playing = False
    except:
        pass
    return 0.1  # เช็คทุก 100ms


# ── Network ────────────────────────────────────

def _client_handler(conn, addr):
    global _client_count
    if not _ws_handshake(conn):
        conn.close(); return
    _client_count += 1
    try:
        while _running:
            msg = _ws_recv_frame(conn)
            if msg is None: break
            try:
                with _data_lock:
                    _latest_data.update(json.loads(msg))
            except: pass
    except: pass
    finally:
        _client_count -= 1
        conn.close()

def _server_loop(port):
    global _server_socket, _running
    _server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        _server_socket.bind(("0.0.0.0", port))
        _server_socket.listen(5)
        _server_socket.settimeout(1.0)
        while _running:
            try:
                conn, addr = _server_socket.accept()
                threading.Thread(target=_client_handler,
                                 args=(conn, addr), daemon=True).start()
            except socket.timeout: continue
    except Exception as e:
        print(f"[PhoneCam] {e}")
    finally:
        _server_socket.close()

def start_server(port=8765):
    global _running
    if _running: return
    _running = True
    threading.Thread(target=_server_loop, args=(port,), daemon=True).start()

def stop_server():
    global _running
    _running = False

# ── Helper: get camera object ──────────────────

def _get_obj():
    try:
        props = bpy.context.scene.phone_cam_props
        obj = bpy.data.objects.get(props.camera_name) if props.camera_name else None
        return obj if obj else bpy.context.scene.camera
    except:
        return None

# ══════════════════════════════════════════════
#  TIMER 1 — ROTATION (runs at 60 fps = 0.016s)
#  Only applies rotation + movement to camera.
#  NEVER touches frames or keyframes.
# ══════════════════════════════════════════════

def _timer_rotation():
    try:
        props = bpy.context.scene.phone_cam_props
    except:
        return 0.016

    with _data_lock:
        data = dict(_latest_data)
        _latest_data.pop("reset", None)

    if data.get("reset"):
        _do_reset(props)
        return 0.016

    obj = _get_obj()
    if not obj:
        return 0.016

    # ── Rotation ──
    if "qw" in data:
        phone_q  = Quaternion((data["qw"], data["qx"], data["qy"], data["qz"]))
        target_q = AXIS_FIX @ phone_q
        obj.rotation_mode = "QUATERNION"
        if props.use_smoothing:
            obj.rotation_quaternion = obj.rotation_quaternion.copy().slerp(
                target_q, props.smoothing)
        else:
            obj.rotation_quaternion = target_q

    # ── Movement ──
    mx = float(data.get("move_x", 0.0))
    my = float(data.get("move_y", 0.0))
    mz = float(data.get("move_z", 0.0))
    sp = props.move_speed
    if abs(mx) > 0.005 or abs(my) > 0.005 or abs(mz) > 0.005:
        rot = (obj.rotation_quaternion.to_matrix()
               if obj.rotation_mode == "QUATERNION"
               else obj.rotation_euler.to_matrix())
        obj.location += rot @ Vector((mx * sp, mz * sp, -my * sp))

    # ── Focal ──
    cam = obj.data if obj.type == "CAMERA" else None
    if cam is None and bpy.context.scene.camera and bpy.context.scene.camera.type == "CAMERA":
        cam = bpy.context.scene.camera.data
    if cam:
        if "zoom_preset" in data:
            cam.lens = {1: 24.0, 3: 70.0, 5: 135.0}.get(data["zoom_preset"], 50.0)
        if "focal_set" in data:
            cam.lens = max(1.0, min(800.0, float(data["focal_set"])))

    # Redraw
    for area in bpy.context.screen.areas:
        if area.type == "VIEW_3D":
            area.tag_redraw()

    return 0.016   # always 60 fps


# ══════════════════════════════════════════════
#  TIMER 2 — KEYFRAME (runs at record fps: 1/24, 1/30, 1/60)
#  Reads camera's CURRENT position (already moved by Timer 1)
#  and bakes it as a keyframe. No rotation logic here.
# ══════════════════════════════════════════════

def _timer_keyframe():
    global _is_recording, _record_frame, _record_start_frame, _record_fps

    if not _is_recording:
        return 1.0 / _record_fps   # sleep until needed

    obj = _get_obj()
    if obj is None:
        return 1.0 / _record_fps

    try:
        props = bpy.context.scene.phone_cam_props
    except:
        return 1.0 / _record_fps

    # Target frame
    abs_frame = _record_start_frame + _record_frame

    # Write keyframe at abs_frame using CURRENT live position
    # DO NOT call frame_set() — just write to F-Curve directly
    obj.keyframe_insert(data_path="location",             frame=abs_frame)
    obj.keyframe_insert(data_path="rotation_quaternion",  frame=abs_frame)

    # Advance timeline scrubber (no data reload)
    bpy.context.scene.frame_current = abs_frame

    _record_frame          += 1
    props.rec_frame_count   = _record_frame

    interval = 1.0 / _record_fps
    return interval


_rot_timer_registered = False
_kf_timer_registered  = False

def register_timers():
    global _rot_timer_registered, _kf_timer_registered
    if not _rot_timer_registered:
        bpy.app.timers.register(_timer_rotation, persistent=True)
        _rot_timer_registered = True
    if not _kf_timer_registered:
        bpy.app.timers.register(_timer_keyframe, persistent=True)
        _kf_timer_registered = True
    if not bpy.app.timers.is_registered(_check_playback):
        bpy.app.timers.register(_check_playback, persistent=True)

def unregister_timers():
    global _rot_timer_registered, _kf_timer_registered
    if _rot_timer_registered and bpy.app.timers.is_registered(_timer_rotation):
        bpy.app.timers.unregister(_timer_rotation)
    if _kf_timer_registered and bpy.app.timers.is_registered(_timer_keyframe):
        bpy.app.timers.unregister(_timer_keyframe)
    if bpy.app.timers.is_registered(_check_playback):
        bpy.app.timers.unregister(_check_playback)
    _rot_timer_registered = _kf_timer_registered = False


def _do_reset(props):
    obj = (bpy.data.objects.get(props.camera_name)
           if props.camera_name else bpy.context.scene.camera)
    if obj:
        obj.rotation_mode       = "QUATERNION"
        obj.rotation_quaternion = Quaternion((1, 0, 0, 0))
        obj.location            = Vector((0, -10, 2))
        if obj.type == "CAMERA":
            obj.data.lens = 50.0
    with _data_lock:
        _latest_data.clear()

# ── Properties ─────────────────────────────────

class PhoneCamProps(bpy.types.PropertyGroup):
    port: bpy.props.IntProperty(name="Port", default=8765, min=1024, max=65535)
    camera_name: bpy.props.StringProperty(name="Camera", default="")
    use_smoothing: bpy.props.BoolProperty(name="Smoothing", default=True)
    smoothing: bpy.props.FloatProperty(
        name="Slerp", default=0.18, min=0.01, max=1.0,
        description="0.18 = best  |  1.0 = instant")
    move_speed: bpy.props.FloatProperty(
        name="Move Speed", default=0.05, min=0.001, max=2.0)
    is_running:      bpy.props.BoolProperty(default=False)
    is_recording:    bpy.props.BoolProperty(default=False)
    rec_frame_count: bpy.props.IntProperty(default=0)
    record_fps: bpy.props.EnumProperty(
        name="FPS",
        items=[("24","24",""),("30","30",""),("60","60","")],
        default="24")

# ── Operators ──────────────────────────────────

class PHONECAM_OT_Start(bpy.types.Operator):
    bl_idname = "phonecam.start"; bl_label = "Start Server"
    def execute(self, context):
        p = context.scene.phone_cam_props
        start_server(p.port)
        register_timers()
        p.is_running = True
        self.report({"INFO"}, f"Server on port {p.port}")
        return {"FINISHED"}

class PHONECAM_OT_Stop(bpy.types.Operator):
    bl_idname = "phonecam.stop"; bl_label = "Stop"
    def execute(self, context):
        global _is_recording
        stop_server(); unregister_timers()
        _is_recording = False
        p = context.scene.phone_cam_props
        p.is_running = p.is_recording = False
        return {"FINISHED"}

class PHONECAM_OT_Reset(bpy.types.Operator):
    bl_idname = "phonecam.reset"; bl_label = "Reset Camera"
    def execute(self, context):
        _do_reset(context.scene.phone_cam_props)
        return {"FINISHED"}

class PHONECAM_OT_StartRec(bpy.types.Operator):
    bl_idname = "phonecam.start_rec"; bl_label = "Start Record"
    def execute(self, context):
        global _is_recording, _record_frame, _record_start_frame, _record_fps
        p = context.scene.phone_cam_props
        _record_fps         = int(p.record_fps)
        _record_frame       = 0
        _record_start_frame = context.scene.frame_current
        context.scene.render.fps = _record_fps
        _is_recording       = True
        p.is_recording      = True
        p.rec_frame_count   = 0
        self.report({"INFO"}, f"REC {_record_fps}fps start={_record_start_frame}")
        return {"FINISHED"}

class PHONECAM_OT_StopRec(bpy.types.Operator):
    bl_idname = "phonecam.stop_rec"; bl_label = "Stop Record"
    def execute(self, context):
        global _is_recording
        _is_recording = False
        p = context.scene.phone_cam_props
        p.is_recording = False
        # Set interpolation to LINEAR for smooth playback (no wobble)
        obj = _get_obj()
        if obj and obj.animation_data and obj.animation_data.action:
            for fc in obj.animation_data.action.fcurves:
                for kp in fc.keyframe_points:
                    kp.interpolation = 'LINEAR'
                fc.update()
        self.report({"INFO"}, f"Recorded {p.rec_frame_count} frames")
        return {"FINISHED"}

# ── Panel ──────────────────────────────────────

class PHONECAM_PT_Panel(bpy.types.Panel):
    bl_label = "📱 Phone Camera v5fix2"
    bl_idname = "PHONECAM_PT_Panel"
    bl_space_type = "VIEW_3D"; bl_region_type = "UI"; bl_category = "Phone Cam"

    def draw(self, context):
        layout = self.layout
        p = context.scene.phone_cam_props

        box = layout.box(); row = box.row()
        if p.is_running:
            row.label(text="● LIVE", icon="RADIOBUT_ON")
            row.label(text=f"Port:{p.port}  C:{_client_count}")
        else:
            row.label(text="○ Offline", icon="RADIOBUT_OFF")

        col = layout.column(align=True)
        col.prop(p, "port")
        col.prop_search(p, "camera_name", bpy.data, "objects",
                        text="Camera", icon="CAMERA_DATA")

        row = layout.row()
        if not p.is_running:
            row.operator("phonecam.start", text="▶ Start", icon="PLAY")
        else:
            row.operator("phonecam.stop",  text="■ Stop",  icon="PAUSE")

        layout.separator()

        sb = layout.box()
        sb.label(text="Smoothing:", icon="MOD_SMOOTH")
        sb.prop(p, "use_smoothing")
        if p.use_smoothing:
            sb.prop(p, "smoothing", slider=True)

        mb = layout.box()
        mb.label(text="Movement:", icon="ORIENTATION_GLOBAL")
        mb.prop(p, "move_speed", slider=True)

        obj = (bpy.data.objects.get(p.camera_name)
               if p.camera_name else context.scene.camera)
        if obj and obj.type == "CAMERA":
            fb = layout.box()
            fb.label(text="Focal Length:", icon="CAMERA_DATA")
            fb.prop(obj.data, "lens", text="mm")

        layout.separator()

        rb = layout.box()
        rb.label(text="Record Keyframes:", icon="REC")
        rb.prop(p, "record_fps", expand=True)
        if not p.is_recording:
            rb.operator("phonecam.start_rec", text="⏺ Start Record", icon="REC")
        else:
            row2 = rb.row(); row2.alert = True
            row2.operator("phonecam.stop_rec",
                          text=f"⏹ Stop  ({p.rec_frame_count} frames)",
                          icon="SNAP_FACE")

        layout.separator()
        layout.operator("phonecam.reset", text="↺ Reset Camera", icon="LOOP_BACK")

        ib = layout.box(); ib.label(text="Connect phone to:", icon="INFO")
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close()
        except: ip = "your-PC-IP"
        ib.label(text=f"ws://{ip}:{p.port}")

# ── Register ───────────────────────────────────

classes = [
    PhoneCamProps,
    PHONECAM_OT_Start, PHONECAM_OT_Stop, PHONECAM_OT_Reset,
    PHONECAM_OT_StartRec, PHONECAM_OT_StopRec,
    PHONECAM_PT_Panel,
]

def register():
    for cls in classes: bpy.utils.register_class(cls)
    bpy.types.Scene.phone_cam_props = bpy.props.PointerProperty(type=PhoneCamProps)

def unregister():
    stop_server(); unregister_timers()
    for cls in reversed(classes): bpy.utils.unregister_class(cls)
    del bpy.types.Scene.phone_cam_props

if __name__ == "__main__": register()
