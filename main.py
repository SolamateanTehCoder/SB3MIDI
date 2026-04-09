import mido
import json
import uuid
import hashlib
import zipfile
import sys
import os

def generate_id():
    return uuid.uuid4().hex

def get_step_sig(step):
    """Creates a unique signature for a musical moment to find exact repeats."""
    n_sigs = tuple(sorted([(n['voice'], n['pitch'], n['duration']) for n in step['notes']]))
    return (n_sigs, step['gap'])

def find_global_repeats(step_list, max_pattern_len=16):
    """Finds repeating patterns in the global timeline (Conductor Track)."""
    i = 0
    result = []
    while i < len(step_list):
        found_repeat = False
        # Try to find a repeating pattern starting from length 1 up to max_pattern_len
        for length in range(1, min(max_pattern_len, (len(step_list) - i) // 2) + 1):
            pattern = step_list[i:i+length]
            p_sig = [get_step_sig(s) for s in pattern]
            count = 1
            
            # Check ahead for matching sequences
            while i + (count + 1) * length <= len(step_list):
                next_seq = step_list[i + count * length : i + (count + 1) * length]
                if [get_step_sig(s) for s in next_seq] == p_sig:
                    count += 1
                else:
                    break
            
            if count > 1:
                result.append(('repeat', {'count': count, 'steps': pattern}))
                i += count * length
                found_repeat = True
                break
        
        if not found_repeat:
            result.append(('single', step_list[i]))
            i += 1
    return result

def midi_to_sb3(midi_path, sb3_path):
    print(f"Loading MIDI: {midi_path}...")
    try:
        mid = mido.MidiFile(midi_path)
    except Exception as e:
        print(f"Error loading MIDI file: {e}")
        return

    # 1. Parse Notes and assign Voices
    active_notes = {}
    notes_by_type = {"melodic": [], "percussion": []}
    current_time = 0.0
    
    for msg in mid:
        current_time += msg.time
        if msg.type in ['note_on', 'note_off']:
            is_perc = (msg.channel == 9) 
            key = (msg.channel, msg.note)
            
            if msg.type == 'note_on' and msg.velocity > 0:
                active_notes[key] = (current_time, msg.velocity)
            elif key in active_notes:
                start_time, velocity = active_notes.pop(key)
                duration = current_time - start_time
                if duration > 0:
                    note_data = {'start': start_time, 'duration': duration, 'pitch': msg.note, 'velocity': velocity}
                    if is_perc: notes_by_type["percussion"].append(note_data)
                    else: notes_by_type["melodic"].append(note_data)

    voices = []
    for n_type in ["melodic", "percussion"]:
        type_notes = sorted(notes_by_type[n_type], key=lambda x: x['start'])
        current_voices = []
        for note in type_notes:
            placed = False
            for v in current_voices:
                if v[-1]['start'] + v[-1]['duration'] <= note['start']:
                    v.append(note)
                    placed = True
                    break
            if not placed:
                current_voices.append([note])
        for v in current_voices:
            voices.append({"is_perc": n_type == "percussion", "notes": v})

    # 2. Build Global Timeline (Events & Steps)
    raw_times = sorted(list(set([round(n['start'], 2) for v in voices for n in v['notes']])))
    events = []
    
    for t in raw_times:
        active = []
        for v_idx, v in enumerate(voices):
            for n in v["notes"]:
                if round(n['start'], 2) == t:
                    active.append({
                        "voice": v_idx,
                        "pitch": n['pitch'],
                        "duration": round(n['duration'], 2)
                    })
        if active:
            events.append({"time": t, "notes": active})
            
    steps = []
    for i in range(len(events)):
        t_current = events[i]["time"]
        t_next = events[i+1]["time"] if i + 1 < len(events) else t_current
        gap = round(t_next - t_current, 2)
        steps.append({"time": t_current, "notes": events[i]["notes"], "gap": gap})

    processed_sequence = find_global_repeats(steps)

    # 3. Setup Broadcast Mappings
    broadcast_map = {} 
    stage_broadcasts = {}
    receivers = {i: [] for i in range(len(voices))}

    def get_or_create_broadcast(n_sigs):
        if n_sigs not in broadcast_map:
            b_name = f"Evt_{len(broadcast_map) + 1}"
            broadcast_map[n_sigs] = b_name
            stage_broadcasts[b_name] = b_name
            for voice_idx, pitch, dur in n_sigs:
                receivers[voice_idx].append({"b_name": b_name, "pitch": pitch, "duration": dur})
        return broadcast_map[n_sigs]

    svg_data = b'<svg version="1.1" width="2" height="2" viewBox="-1 -1 2 2" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"></svg>'
    svg_md5 = hashlib.md5(svg_data).hexdigest()
    svg_filename = f"{svg_md5}.svg"

    # 4. Generate Stage Blocks (The Conductor)
    stage_blocks = {}
    s_last_id = generate_id()
    stage_blocks[s_last_id] = {"opcode": "event_whenflagclicked", "next": None, "parent": None, "inputs": {}, "fields": {}, "shadow": False, "topLevel": True, "x": 10, "y": 10}

    for item_type, data in processed_sequence:
        if item_type == 'single':
            b_name = get_or_create_broadcast(get_step_sig(data)[0])
            b_id = generate_id()
            stage_blocks[s_last_id]["next"] = b_id
            stage_blocks[b_id] = {"opcode": "event_broadcast", "next": None, "parent": s_last_id, "inputs": {"BROADCAST_INPUT": [1, [11, b_name, b_name]]}, "fields": {}, "shadow": False}
            s_last_id = b_id
            
            if data['gap'] > 0.01:
                r_id = generate_id()
                stage_blocks[s_last_id]["next"] = r_id
                stage_blocks[r_id] = {"opcode": "music_restForBeats", "next": None, "parent": s_last_id, "inputs": {"BEATS": [1, [4, str(data['gap'])]]}, "fields": {}, "shadow": False}
                s_last_id = r_id
        else:
            rep_id = generate_id()
            stage_blocks[s_last_id]["next"] = rep_id
            stage_blocks[rep_id] = {"opcode": "control_repeat", "next": None, "parent": s_last_id, "inputs": {"TIMES": [1, [4, str(data['count'])]], "SUBSTACK": [2, None]}, "fields": {}, "shadow": False}
            
            sub_last = None
            for idx, step in enumerate(data['steps']):
                b_name = get_or_create_broadcast(get_step_sig(step)[0])
                b_id = generate_id()
                
                if sub_last is None:
                    stage_blocks[rep_id]["inputs"]["SUBSTACK"][1] = b_id
                    stage_blocks[b_id] = {"opcode": "event_broadcast", "next": None, "parent": rep_id, "inputs": {"BROADCAST_INPUT": [1, [11, b_name, b_name]]}, "fields": {}, "shadow": False}
                else:
                    stage_blocks[sub_last]["next"] = b_id
                    stage_blocks[b_id] = {"opcode": "event_broadcast", "next": None, "parent": sub_last, "inputs": {"BROADCAST_INPUT": [1, [11, b_name, b_name]]}, "fields": {}, "shadow": False}
                sub_last = b_id
                
                if step['gap'] > 0.01:
                    r_id = generate_id()
                    stage_blocks[sub_last]["next"] = r_id
                    stage_blocks[r_id] = {"opcode": "music_restForBeats", "next": None, "parent": sub_last, "inputs": {"BEATS": [1, [4, str(step['gap'])]]}, "fields": {}, "shadow": False}
                    sub_last = r_id
                    
            s_last_id = rep_id

    targets = [{
        "isStage": True, "name": "Stage", "variables": {}, "lists": {}, "broadcasts": stage_broadcasts, "blocks": stage_blocks, "comments": {},
        "currentCostume": 0, "costumes": [{"assetId": svg_md5, "name": "backdrop1", "md5ext": svg_filename, "dataFormat": "svg", "rotationCenterX": 240, "rotationCenterY": 180}],
        "sounds": [], "volume": 100, "layerOrder": 0, "tempo": 60
    }]

    # 5. Generate Voice Sprites (Receivers & Volume Threads)
    for v_idx, voice_data in enumerate(voices):
        v_blocks = {}
        
        # --- PARALLEL VOLUME THREAD ---
        vol_start = generate_id()
        v_blocks[vol_start] = {"opcode": "event_whenflagclicked", "next": None, "parent": None, "inputs": {}, "fields": {}, "shadow": False, "topLevel": True, "x": 10, "y": 10}
        vol_last = vol_start
        
        vol_map = {}
        for n in voice_data['notes']:
            vol_map[round(n['start'], 2)] = round((n['velocity'] / 127) * 100)
            
        current_vol = 100
        vol_time = 0.0
        for t in sorted(vol_map.keys()):
            target_vol = vol_map[t]
            if target_vol != current_vol:
                gap = round(t - vol_time, 2)
                if gap > 0.02:
                    r_id = generate_id()
                    v_blocks[vol_last]["next"] = r_id
                    v_blocks[r_id] = {"opcode": "music_restForBeats", "next": None, "parent": vol_last, "inputs": {"BEATS": [1, [4, str(gap)]]}, "fields": {}, "shadow": False}
                    vol_last = r_id
                
                v_id = generate_id()
                v_blocks[vol_last]["next"] = v_id
                v_blocks[v_id] = {"opcode": "sound_setvolumeto", "next": None, "parent": vol_last, "inputs": {"VOLUME": [1, [4, str(target_vol)]]}, "fields": {}, "shadow": False}
                vol_last = v_id
                current_vol = target_vol
                vol_time = t

        # --- SETUP THREAD ---
        setup_id = generate_id()
        v_blocks[setup_id] = {"opcode": "event_whenflagclicked", "next": None, "parent": None, "inputs": {}, "fields": {}, "shadow": False, "topLevel": True, "x": 300, "y": 10}
        if not voice_data['is_perc']:
            inst_id = generate_id()
            menu_id = generate_id()
            v_blocks[setup_id]["next"] = inst_id
            v_blocks[inst_id] = {"opcode": "music_setInstrument", "next": None, "parent": setup_id, "inputs": {"INSTRUMENT": [1, menu_id]}, "fields": {}, "shadow": False}
            v_blocks[menu_id] = {"opcode": "music_menu_INSTRUMENT", "next": None, "parent": inst_id, "fields": {"INSTRUMENT": ["1", None]}, "shadow": True}

        # --- BROADCAST RECEIVERS (The Notes) ---
        y_offset = 200
        for rx in receivers[v_idx]:
            hat_id = generate_id()
            play_id = generate_id()
            
            v_blocks[hat_id] = {
                "opcode": "event_whenbroadcastreceived", "next": play_id, "parent": None, "inputs": {},
                "fields": {"BROADCAST_OPTION": [rx['b_name'], rx['b_name']]}, "shadow": False, "topLevel": True, "x": 10, "y": y_offset
            }
            
            if voice_data['is_perc']:
                drum_val = (rx['pitch'] % 18) + 1
                v_blocks[play_id] = {
                    "opcode": "music_playDrumForBeats", "next": None, "parent": hat_id,
                    "inputs": {"DRUM": [1, [4, str(drum_val)]], "BEATS": [1, [4, str(rx['duration'])]]}, "fields": {}, "shadow": False
                }
            else:
                v_blocks[play_id] = {
                    "opcode": "music_playNoteForBeats", "next": None, "parent": hat_id,
                    "inputs": {"NOTE": [1, [4, str(rx['pitch'])]], "BEATS": [1, [4, str(rx['duration'])]]}, "fields": {}, "shadow": False
                }
            y_offset += 150

        sprite_name = f"Drum_{v_idx+1}" if voice_data["is_perc"] else f"Voice_{v_idx+1}"
        targets.append({
            "isStage": False, "name": sprite_name, "variables": {}, "lists": {}, "broadcasts": {},
            "blocks": v_blocks, "comments": {}, "currentCostume": 0,
            "costumes": [{"assetId": svg_md5, "name": "costume1", "md5ext": svg_filename, "dataFormat": "svg", "rotationCenterX": 0, "rotationCenterY": 0}],
            "sounds": [], "volume": 100, "layerOrder": v_idx + 1, "visible": True, "x": 0, "y": 0, "size": 100, "direction": 90
        })

    # 6. Export
    project = {"targets": targets, "monitors": [], "extensions": ["music"], "meta": {"semver": "3.0.0", "vm": "0.2.0", "agent": "Python MIDI Generator"}}
    print("Generating .sb3 file...")
    with zipfile.ZipFile(sb3_path, 'w', zipfile.ZIP_DEFLATED) as sb3:
        sb3.writestr('project.json', json.dumps(project))
        sb3.writestr(svg_filename, svg_data)
        
    print(f"Success! Output saved to: {sb3_path}")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python midi2scratch.py <input.mid> <output.sb3>")
        sys.exit(1)
    if not os.path.exists(sys.argv[1]):
        print(f"Error: File '{sys.argv[1]}' not found.")
        sys.exit(1)
    midi_to_sb3(sys.argv[1], sys.argv[2])
