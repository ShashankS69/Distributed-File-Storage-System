from flask import Flask, render_template, request, jsonify, send_file
from flask_socketio import SocketIO, emit
import os, math, json, time, threading
from werkzeug.utils import secure_filename
from datetime import datetime

app = Flask(__name__)
socketio = SocketIO(app)

# Change volume paths to local directories
VOLUME1_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'volume1')
VOLUME2_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'volume2')

# Create directories if they don't exist
os.makedirs(VOLUME1_PATH, exist_ok=True)
os.makedirs(VOLUME2_PATH, exist_ok=True)

CHUNK_SIZE = 1024 * 1024  # 1MB chunks

# Ricart-Agrawala implementation
class RicartAgrawala:
    def __init__(self):
        self.timestamp = 0
        self.requesting = False
        self.replies_received = set()
        self.deferred_replies = []
        self.lock = threading.Lock()

    def request_cs(self, node_id):
        with self.lock:
            self.requesting = True
            self.timestamp = time.time()
            self.replies_received = set()
            return self.timestamp

    def release_cs(self):
        with self.lock:
            self.requesting = False
            for node in self.deferred_replies:
                socketio.emit('reply', {'timestamp': self.timestamp}, room=node)
            self.deferred_replies = []

ra = RicartAgrawala()

class SystemMetrics:
    def __init__(self):
        self.request_history = []
        self.cs_usage = []
        self.node_interactions = {}
        self.nodes = set()

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

    def get_metrics(self):
        return {
            'request_history': self.request_history[-20:],  # Last 20 events
            'cs_usage': self.cs_usage[-10:],  # Last 10 CS usages
            'node_interactions': self.node_interactions,
            'nodes': list(self.nodes)
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
    return render_template('visualize.html', storage_info=get_volume_info())

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

@app.errorhandler(404)
def page_not_found(e):
    return render_template('error.html', error="Page not found"), 404

@socketio.on('connect')
def handle_connect():
    emit('storage_update', get_volume_info())

@socketio.on('request_cs')
def handle_cs_request(data):
    node_id = request.sid
    timestamp = ra.request_cs(node_id)
    metrics.log_request(node_id, 'system', timestamp)
    socketio.emit('storage_update', {
        **get_volume_info(),
        'metrics': metrics.get_metrics()
    })

if __name__ == '__main__':
    socketio.run(app, debug=True)
