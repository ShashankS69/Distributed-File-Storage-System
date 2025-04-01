# Distributed File Storage System

## Features
- Distributed file storage across multiple volumes
- Real-time synchronization using Ricart-Agrawala algorithm
- Real-time monitoring with Socket.IO
- Fault tolerance and health monitoring
- File chunk distribution and retrieval
- Interactive visualization dashboard

## Setup Instructions

1. Create a virtual environment:
```bash
python -m venv venv
```

2. Activate the virtual environment:
- On Windows:
```bash
venv\Scripts\activate
```
- On macOS/Linux:
```bash
source venv/bin/activate
```

3. Install requirements:
```bash
pip install -r requirements.txt
```

4. Run the application:
```bash
python app.py
```

5. Access the application at http://localhost:5000

## Fault Tolerance
The system implements:
- Volume health monitoring
- Automatic failover for chunk storage
- Real-time health status visualization
- Distributed mutual exclusion for concurrent access

## Architecture
- Flask backend with Socket.IO for real-time updates
- Ricart-Agrawala algorithm for distributed synchronization
- ChartJS for real-time visualization
- Distributed file chunking and storage
