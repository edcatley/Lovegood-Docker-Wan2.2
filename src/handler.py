import runpod
import time
import requests
import os

def handler(job):
    """
    An asynchronous handler that simulates a multi-job video generation task.
    It receives a list of jobs, simulates a delay for each, and sends a
    callback to a specified URL upon completion of each job.
    """
    job_input = job['input']

    # --- 1. Input Validation ---
    # In an async flow, it's crucial to validate input before you start.
    image_base64 = job_input.get('image_base64')
    jobs = job_input.get('jobs')
    callback_url = job_input.get('callback_url')

    if not all([image_base64, jobs, callback_url]):
        # Even in async, you can return an error if the job is malformed.
        # This will show up in the job status.
        return {
            "error": "Missing one or more required fields: image_base64, jobs, callback_url"
        }

    print(f"Received {len(jobs)} jobs to process. Callback URL: {callback_url}")

    # --- 2. Process Each Job Sequentially ---
    # The worker will process this loop in the background.
    for job_item in jobs:
        job_id = job_item.get('job_id')
        prompt = job_item.get('prompt')

        if not job_id:
            print("Skipping a job item due to missing 'job_id'.")
            continue

        print(f"Processing job_id: {job_id} with prompt: '{prompt}'")

        # --- 3. Simulate AI Generation Delay ---
        # Replace this with your actual ComfyUI call in the future.
        print(f"[{job_id}] Simulating video generation... (sleeping for 15 seconds)")
        time.sleep(15)
        print(f"[{job_id}] Simulation complete.")

        # --- 4. Send the Callback ---
        # Construct the payload to send back to your main application.
        callback_payload = {
            "job_id": job_id,
            "status": "completed",
            # In the real version, you would add the GCS URL here
            "video_url": f"https://storage.googleapis.com/your-bucket/videos/{job_id}.mp4" # Placeholder
        }

        print(f"[{job_id}] Sending callback to {callback_url}")
        try:
            requests.post(callback_url, json=callback_payload, timeout=10)
            print(f"[{job_id}] Callback sent successfully.")
        except requests.exceptions.RequestException as e:
            # If the callback fails, log the error but don't crash the worker.
            # It can continue with the next job.
            print(f"[{job_id}] ERROR: Could not send callback: {e}")

    # The return value of an async handler is mainly for RunPod's internal tracking.
    return {
        "status": "completed",
        "jobs_processed": len(jobs)
    }


# Start the serverless worker.
# We don't need `start_async` here; the RunPod platform handles the async
# nature based on how you call the API (/run vs /runsync).
runpod.serverless.start({"handler": handler})