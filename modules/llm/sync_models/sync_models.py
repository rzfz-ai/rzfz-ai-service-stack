# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
import time
import os
import requests
import psycopg2
import json
import logging
from datetime import datetime

# Configuration via Environment Variables
GPUSTACK_URL = os.getenv('GPUSTACK_URL', 'http://gpustack:9090/v1-openai/models')
GPUSTACK_API_KEY = os.getenv('GPUSTACK_API_KEY', '') # New API Key variable
DB_HOST = os.getenv('DB_HOST', 'openwebui-db')
DB_NAME = os.getenv('DB_NAME', 'openwebui')
DB_USER = os.getenv('DB_USER', 'postgres')
DB_PASS = os.getenv('DB_PASS', 'password')
POLL_INTERVAL = int(os.getenv('POLL_INTERVAL', '60'))

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def get_db_connection():
    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASS
        )
        return conn
    except Exception as e:
        logger.error(f"Error connecting to database: {e}")
        return None

def get_first_user_id(cursor):
    """
    Fetches the first user ID to assign ownership of the new models.
    """
    try:
        cursor.execute("SELECT id FROM \"user\" ORDER BY created_at ASC LIMIT 1;")
        result = cursor.fetchone()
        if result:
            return result[0]
        return None
    except Exception as e:
        logger.error(f"Error fetching user ID: {e}")
        return None

def fetch_gpustack_models():
    try:
        headers = {}
        if GPUSTACK_API_KEY:
            headers['Authorization'] = f"Bearer {GPUSTACK_API_KEY}"

        response = requests.get(GPUSTACK_URL, headers=headers)
        
        if response.status_code == 200:
            data = response.json()
            return [model['id'] for model in data.get('data', [])]
        elif response.status_code == 401:
            logger.error("Unauthorized: Invalid GPUStack API Key.")
            return []
        else:
            logger.error(f"Failed to fetch models from GPUStack: {response.status_code}")
            return []
    except Exception as e:
        logger.error(f"Error connecting to GPUStack: {e}")
        return []

def sync_models():
    conn = get_db_connection()
    if not conn:
        return

    try:
        cur = conn.cursor()
        
        # 1. Get available models from GPUStack
        gpu_models = fetch_gpustack_models()
        if not gpu_models:
            logger.info("No models found in GPUStack (or connection failed).")
            return

        # 2. Get existing models from OpenWebUI DB
        cur.execute("SELECT id FROM model;")
        existing_models = {row[0] for row in cur.fetchall()}

        # 3. Determine missing models
        missing_models = [m for m in gpu_models if m not in existing_models]

        if not missing_models:
            # logger.info("No new models to sync.") 
            # Commented out to reduce log spam on every poll
            return

        # 4. Get a valid user ID for ownership
        user_id = get_first_user_id(cur)
        if not user_id:
            logger.warning("No users found in DB. Cannot insert models without an owner.")
            return

        # 5. Insert missing models
        timestamp = int(time.time())
        
        for model_id in missing_models:
            logger.info(f"Inserting new model: {model_id}")
            
            # Column order in OpenWebUI >= 0.8.8:
            # id, user_id, base_model_id, name, meta, params, is_active, created_at, updated_at
            # Note: 'access_control' column was removed in 0.8.8
            query = """
                INSERT INTO model (
                    id, user_id, base_model_id, name, meta, params, is_active, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            
            default_meta = {
                "profile_image_url": "/static/favicon.png",
                "description": None,
                "capabilities": {
                    "file_context": True,
                    "vision": True,
                    "file_upload": True,
                    "web_search": True,
                    "image_generation": True,
                    "code_interpreter": True,
                    "citations": True,
                    "status_updates": True,
                    "builtin_tools": True
                }
            }
            default_params = {}
            
            meta_json = json.dumps(default_meta)
            params_json = json.dumps(default_params)
            
            cur.execute(query, (
                model_id,           # id
                user_id,            # user_id
                None,               # base_model_id
                model_id,           # name
                meta_json,          # meta
                params_json,        # params
                True,               # is_active
                timestamp,          # created_at
                timestamp           # updated_at
            ))
        
        conn.commit()
        logger.info(f"Successfully synced {len(missing_models)} models.")

    except Exception as e:
        logger.error(f"Sync process failed: {e}")
        conn.rollback()
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    logger.info("Starting GPUStack Model Sync Service...")
    while True:
        sync_models()
        time.sleep(POLL_INTERVAL)
