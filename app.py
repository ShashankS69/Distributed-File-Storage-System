from flask import Flask, render_template, request, jsonify, send_file
from flask_socketio import SocketIO, emit
import os
import math
import json
import time
import threading
from werkzeug.utils import secure_filename
from datetime import datetime

app = Flask(__name__)
socketio = SocketIO(app)

# Change volume paths to local directories
VOLUME1_PATH = os.path.join(os.path.dirname(
    os.path.abspath(__file__)), 'data', 'volume1')
VOLUME2_PATH = os.path.join(os.path.dirname(
    os.path.abspath(__file__)), 'data', 'volume2')

# Create directories if they don't exist
os.makedirs(VOLUME1_PATH, exist_ok=True)
os.makedirs(VOLUME2_PATH, exist_ok=True)

CHUNK_SIZE = 1024 * 1024


class LamportClock:
    def __init__(self):
        self.value = 0
        self.lock = threading.Lock()

    def get_time(self):
        with self.lock:
            return self.value

    def increment(self):
        with self.lock:
            self.value += 1
            return self.value

    def update(self, received_time):
        with self.lock:
            self.value = max(self.value, received_time) + 1
            return self.value


class RicartAgrawala:
    def __init__(self):
        self.clock = LamportClock()
        self.requesting = False
        self.in_critical_section = False
        self.request_timestamp = 0
        self.replies_received = set()
        self.deferred_replies = []
        self.lock = threading.Lock()
        self.nodes = set()  # Track all nodes in the system
        self.health_status = {
            'volume1': True,
            'volume2': True
        }
        # Track queue of nodes waiting for the critical section
        self.waiting_queue = []
        # Current node in critical section (if any)
        self.current_cs_node = None

    def request_cs(self, node_id):
        with self.lock:
            self.requesting = True
            # Increment the logical clock when sending a message
            self.request_timestamp = self.clock.increment()
            self.replies_received = set()

            # Add self to waiting queue
            if node_id not in self.waiting_queue:
                self.waiting_queue.append(node_id)

            # Broadcast request to all nodes
            for node in self.nodes:
                if node != node_id:
                    socketio.emit('cs_request', {
                        'from_node': node_id,
                        'timestamp': self.request_timestamp
                    }, room=node)

            # If we're the only node, we can enter CS immediately
            if len(self.nodes) <= 1:
                self.in_critical_section = True
                self.current_cs_node = node_id
                metrics.log_cs_entry(node_id, self.request_timestamp)
                return self.request_timestamp

            # Return current timestamp
            return self.request_timestamp

    def receive_request(self, from_node, request_timestamp, my_node_id):
        with self.lock:
            # Update clock based on received timestamp
            self.clock.update(request_timestamp)

            # Add requesting node to waiting queue if not already there
            if from_node not in self.waiting_queue:
                self.waiting_queue.append(from_node)

            # Check if we should defer the reply
            # If we're requesting and have higher priority (lower timestamp or same timestamp but lower ID)
            # or if we're already in critical section
            should_defer = False

            if self.in_critical_section:
                should_defer = True
            elif self.requesting and (
                self.request_timestamp < request_timestamp or
                (self.request_timestamp == request_timestamp and my_node_id < from_node)
            ):
                should_defer = True

            if should_defer:
                self.deferred_replies.append(from_node)
                metrics.log_request_deferred(
                    my_node_id, from_node, self.clock.get_time())
            else:
                # Reply immediately - increment clock for sending message
                reply_timestamp = self.clock.increment()
                socketio.emit('cs_reply', {
                    'from_node': my_node_id,
                    'timestamp': reply_timestamp
                }, room=from_node)
                metrics.log_request_granted(
                    my_node_id, from_node, reply_timestamp)

    def receive_reply(self, from_node, received_timestamp):
        with self.lock:
            # Update clock based on received timestamp
            self.clock.update(received_timestamp)

            self.replies_received.add(from_node)
            # If we have all replies, we can enter the critical section
            if self.requesting and len(self.replies_received) >= len(self.nodes) - 1:
                # Enter critical section
                self.requesting = False
                self.in_critical_section = True
                self.current_cs_node = request.sid
                metrics.log_cs_entry(request.sid, self.clock.get_time())

                # Update health status
                self.update_health_status()

    def release_cs(self):
        with self.lock:
            if not self.in_critical_section:
                return

            # Exit critical section
            self.in_critical_section = False
            node_id = self.current_cs_node
            self.current_cs_node = None

            # Log CS exit
            exit_timestamp = self.clock.increment()
            metrics.log_cs_exit(node_id, exit_timestamp)

            # Remove the node from waiting queue
            if node_id in self.waiting_queue:
                self.waiting_queue.remove(node_id)

            # Send deferred replies
            for node in self.deferred_replies:
                # Increment clock for each message sent
                reply_timestamp = self.clock.increment()
                socketio.emit('cs_reply', {
                    'from_node': node_id,
                    'timestamp': reply_timestamp
                }, room=node)
                metrics.log_request_granted(node_id, node, reply_timestamp)

            self.deferred_replies = []

    def update_health_status(self):
        # Check health of volumes and broadcast to all nodes
        health_status = check_volumes_health()
        socketio.emit('health_update', health_status)
        self.health_status = health_status
        return health_status

    def register_node(self, node_id):
        with self.lock:
            self.nodes.add(node_id)
            return len(self.nodes)

    def unregister_node(self, node_id):
        with self.lock:
            if node_id in self.nodes:
                self.nodes.remove(node_id)
            if node_id in self.replies_received:
                self.replies_received.remove(node_id)
            if node_id in self.waiting_queue:
                self.waiting_queue.remove(node_id)
            if self.current_cs_node == node_id:
                self.current_cs_node = None
                self.in_critical_section = False
            return len(self.nodes)

    def get_mutex_state(self):
        with self.lock:
            return {
                'in_critical_section': self.in_critical_section,
                'current_cs_node': self.current_cs_node,
                'waiting_queue': list(self.waiting_queue),
                'logical_clock': self.clock.get_time()
            }


ra = RicartAgrawala()


class SystemMetrics:
    def __init__(self):
        self.request_history = []
        self.cs_usage = []
        self.node_interactions = {}
        self.nodes = set()
        self.mutex_events = []
        self.logical_clock_history = []

    def log_request(self, from_node, to_node, timestamp):
        self.nodes.add(from_node)
        self.nodes.add(to_node)
        self.request_history.append({
            'from': from_node,
            'to': to_node,
            'timestamp': timestamp,
            'type': 'request'
        })

        # Update interaction heatmap
        key = f"{from_node}-{to_node}"
        self.node_interactions[key] = self.node_interactions.get(key, 0) + 1

    def log_cs_entry(self, node, timestamp):
        self.cs_usage.append({
            'node': node,
            'action': 'enter',
            'timestamp': timestamp,
            'time': datetime.now().isoformat()
        })
        self.mutex_events.append({
            'node': node,
            'action': 'enter',
            'timestamp': timestamp,
            'time': datetime.now().isoformat()
        })

    def log_cs_exit(self, node, timestamp):
        self.cs_usage.append({
            'node': node,
            'action': 'exit',
            'timestamp': timestamp,
            'time': datetime.now().isoformat()
        })
        self.mutex_events.append({
            'node': node,
            'action': 'exit',
            'timestamp': timestamp,
            'time': datetime.now().isoformat()
        })

    def log_request_granted(self, from_node, to_node, timestamp):
        self.mutex_events.append({
            'from': from_node,
            'to': to_node,
            'action': 'grant',
            'timestamp': timestamp,
            'time': datetime.now().isoformat()
        })

    def log_request_deferred(self, from_node, to_node, timestamp):
        self.mutex_events.append({
            'from': from_node,
            'to': to_node,
            'action': 'defer',
            'timestamp': timestamp,
            'time': datetime.now().isoformat()
        })

    def log_clock_update(self, node, value, reason):
        self.logical_clock_history.append({
            'node': node,
            'value': value,
            'reason': reason,
            'time': datetime.now().isoformat()
        })

    def get_metrics(self):
        return {
            'request_history': self.request_history[-20:],  # Last 20 events
            'cs_usage': self.cs_usage[-10:],  # Last 10 CS usages
            'node_interactions': self.node_interactions,
            'nodes': list(self.nodes)
        }

    def get_mutex_metrics(self):
        return {
            'mutex_events': self.mutex_events[-20:],
            'cs_usage': self.cs_usage[-10:]
        }

    def get_clock_metrics(self):
        return {
            'clock_history': self.logical_clock_history[-20:]
        }


metrics = SystemMetrics()


def get_volume_info():
    info = {
        'volume1': {'total': 2 * 1024 * 1024 * 1024, 'used': 0, 'free': 0, 'files': []},
        'volume2': {'total': 3 * 1024 * 1024 * 1024, 'used': 0, 'free': 0, 'files': []},
        'timestamp': datetime.now().isoformat(),
        'health_status': check_volumes_health()
    }

    vol1_used = sum(os.path.getsize(os.path.join(VOLUME1_PATH, f))
                    for f in os.listdir(VOLUME1_PATH) if os.path.isfile(os.path.join(VOLUME1_PATH, f)))
    vol2_used = sum(os.path.getsize(os.path.join(VOLUME2_PATH, f))
                    for f in os.listdir(VOLUME2_PATH) if os.path.isfile(os.path.join(VOLUME2_PATH, f)))

    info['volume1']['used'] = vol1_used
    info['volume1']['free'] = info['volume1']['total'] - vol1_used
    info['volume2']['used'] = vol2_used
    info['volume2']['free'] = info['volume2']['total'] - vol2_used

    return info


def check_volumes_health():
    return {
        'volume1': os.access(VOLUME1_PATH, os.R_OK | os.W_OK),
        'volume2': os.access(VOLUME2_PATH, os.R_OK | os.W_OK)
    }


def get_available_files():
    files = []
    try:
        # Get all metadata files
        for filename in os.listdir(VOLUME1_PATH):
            if filename.endswith('.meta'):
                with open(os.path.join(VOLUME1_PATH, filename), 'r') as meta_file:
                    metadata = json.load(meta_file)
                    files.append({
                        'filename': metadata['filename'],
                        'size': metadata['total_size'],
                        'chunks': len(metadata['chunks'])
                    })
        return files
    except Exception as e:
        print(f"Error reading files: {str(e)}")
        return []


@app.route('/')
def index():
    return render_template('upload.html')


@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    filename = secure_filename(file.filename)
    file_size = len(file.read())
    file.seek(0)

    volume_info = get_volume_info()

    # Calculate chunks based on available space
    total_chunks = math.ceil(file_size / CHUNK_SIZE)

    # Store file metadata
    metadata = {
        'filename': filename,
        'total_size': file_size,
        'chunks': []
    }

    for i in range(total_chunks):
        chunk = file.read(CHUNK_SIZE)
        chunk_filename = f"{filename}.part{i}"

        # Decide which volume to use based on available space
        if volume_info['volume1']['free'] > len(chunk):
            save_path = os.path.join(VOLUME1_PATH, chunk_filename)
            volume = 'volume1'
        elif volume_info['volume2']['free'] > len(chunk):
            save_path = os.path.join(VOLUME2_PATH, chunk_filename)
            volume = 'volume2'
        else:
            return jsonify({'error': 'Not enough space available'}), 400

        with open(save_path, 'wb') as chunk_file:
            chunk_file.write(chunk)

        metadata['chunks'].append({
            'part': i,
            'volume': volume,
            'filename': chunk_filename
        })

        # Update available space
        volume_info[volume]['free'] -= len(chunk)

    # Save metadata
    with open(os.path.join(VOLUME1_PATH, f"{filename}.meta"), 'w') as meta_file:
        json.dump(metadata, meta_file)

    return jsonify({'success': True, 'message': 'File uploaded successfully'})


@app.route('/delete/<filename>', methods=['DELETE'])
def delete_file(filename):
    try:
        # Read metadata
        with open(os.path.join(VOLUME1_PATH, f"{filename}.meta"), 'r') as meta_file:
            metadata = json.load(meta_file)

        # Delete chunks
        for chunk in metadata['chunks']:
            volume_path = VOLUME1_PATH if chunk['volume'] == 'volume1' else VOLUME2_PATH
            os.remove(os.path.join(volume_path, chunk['filename']))

        # Delete metadata
        os.remove(os.path.join(VOLUME1_PATH, f"{filename}.meta"))
        return jsonify({'success': True})
    except:
        return jsonify({'error': 'File not found'}), 404


@app.route('/visualize')
def visualize():
    files = get_available_files()
    storage_info = get_volume_info()
    # Add file distribution information
    storage_info['file_count'] = len(files)

    # Calculate distribution across volumes
    vol1_count = 0
    vol2_count = 0
    for file in files:
        meta_path = os.path.join(VOLUME1_PATH, f"{file['filename']}.meta")
        if os.path.exists(meta_path):
            with open(meta_path, 'r') as meta_file:
                metadata = json.load(meta_file)
                for chunk in metadata['chunks']:
                    if chunk['volume'] == 'volume1':
                        vol1_count += 1
                    else:
                        vol2_count += 1

    storage_info['distribution'] = {
        'volume1': vol1_count,
        'volume2': vol2_count
    }

    return render_template('visualize.html', storage_info=storage_info)


@app.route('/download/<filename>')
def download_file(filename):
    try:
        with open(os.path.join(VOLUME1_PATH, f"{filename}.meta"), 'r') as meta_file:
            metadata = json.load(meta_file)

        # Reassemble file from chunks
        temp_path = os.path.join(VOLUME1_PATH, 'temp_' + filename)
        with open(temp_path, 'wb') as output_file:
            for chunk in sorted(metadata['chunks'], key=lambda x: x['part']):
                volume_path = VOLUME1_PATH if chunk['volume'] == 'volume1' else VOLUME2_PATH
                chunk_path = os.path.join(volume_path, chunk['filename'])
                with open(chunk_path, 'rb') as chunk_file:
                    output_file.write(chunk_file.read())

        return send_file(temp_path, as_attachment=True, download_name=filename)
    except Exception as e:
        return jsonify({'error': str(e)}), 404
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@app.route('/retrieve')
def retrieve():
    files = get_available_files()
    return render_template('retrieve.html', files=files)


@app.route('/files')
def list_files():
    files = get_available_files()
    return jsonify(files)


@app.route('/api/metrics')
def get_metrics():
    return jsonify(metrics.get_metrics())


@app.route('/api/mutex_state')
def get_mutex_state():
    return jsonify(ra.get_mutex_state())


@app.route('/api/mutex_metrics')
def get_mutex_metrics():
    return jsonify(metrics.get_mutex_metrics())


@app.route('/api/clock_metrics')
def get_clock_metrics():
    return jsonify(metrics.get_clock_metrics())


@app.errorhandler(404)
def page_not_found(e):
    return render_template('error.html', error="Page not found"), 404


@socketio.on('connect')
def handle_connect():
    # Register the connecting node
    node_id = request.sid
    ra.register_node(node_id)
    emit('storage_update', get_volume_info())


@socketio.on('disconnect')
def handle_disconnect():
    # Unregister the disconnecting node
    node_id = request.sid
    ra.unregister_node(node_id)


@socketio.on('cs_request')
def handle_cs_request(data):
    # Handle request for critical section
    from_node = data.get('from_node')
    timestamp = data.get('timestamp')
    my_node_id = request.sid
    ra.receive_request(from_node, timestamp, my_node_id)


@socketio.on('cs_reply')
def handle_cs_reply(data):
    # Handle reply to critical section request
    from_node = data.get('from_node')
    timestamp = data.get('timestamp')
    # Update with the timestamp from the reply
    ra.receive_reply(from_node, timestamp)


@socketio.on('release_cs')
def handle_release_cs():
    ra.release_cs()


@socketio.on('request_health_sync')
def handle_health_sync():
    # Initiate health synchronization using Ricart-Agrawala
    node_id = request.sid
    timestamp = ra.request_cs(node_id)
    metrics.log_request(node_id, 'health-sync', timestamp)
    # After receiving all replies, the health update will happen in receive_reply


@socketio.on('health_update')
def handle_health_update(data):
    # Update local health status based on received data
    if ra.health_status != data:
        ra.health_status = data
        socketio.emit('storage_update', {
            **get_volume_info(),
            'health_status': data
        })


if __name__ == '__main__':
    socketio.run(app, debug=True, port=6969, allow_unsafe_werkzeug=True)
