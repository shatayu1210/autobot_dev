import json
import random
import os

def extract_samples(input_path, output_path, n=10):
    if not os.path.exists(input_path):
        print(f"Error: {input_path} not found.")
        return

    print(f"Reading from {input_path}...")
    with open(input_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    if len(lines) == 0:
        print("Error: Input file is empty.")
        return

    if len(lines) < n:
        print(f"Warning: Only {len(lines)} lines found, taking all.")
        n = len(lines)
        
    samples = random.sample(lines, n)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        for line in samples:
            f.write(line)
            
    print(f"Successfully extracted {n} samples to {output_path}")

if __name__ == "__main__":
    # Get the directory of the current script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Default paths for your local setup
    input_file = os.path.join(script_dir, "prs_clean.jsonl")
    output_file = os.path.join(script_dir, "sample_prs.jsonl")
    
    extract_samples(input_file, output_file)
