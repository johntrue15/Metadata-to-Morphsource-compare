import os
import subprocess
import argparse

def ensure_dir(directory):
    """Make sure the directory exists"""
    if not os.path.exists(directory):
        os.makedirs(directory)

def main():
    # Parse arguments
    parser = argparse.ArgumentParser(description='Run Morphosource data comparison and verification locally')
    parser.add_argument('--csv', default='UF Lepidosaura All CT Scan List Compare.csv', 
                        help='CSV file to compare against (in data/csv/)')
    parser.add_argument('--api-key', default='', 
                        help='Morphosource API key (optional, needed for verification)')
    args = parser.parse_args()
    
    # Ensure output directory exists
    ensure_dir('data/output')
    
    # Set up environment for verification script
    env = os.environ.copy()
    if args.api_key:
        env['MORPHOSOURCE_API_KEY'] = args.api_key
    
    # Run comparison script
    print(f"Running comparison with {args.csv}")
    compare_proc = subprocess.Popen(
        ['python', 'compare.py'],
        stdin=subprocess.PIPE,
        text=True
    )
    
    # Send inputs to comparison script
    inputs = [
        'data/json/morphosource_data_complete_compare.json',
        f'data/csv/{args.csv}',
        'data/output/matched.csv',
        'n'  # No to interactive review
    ]
    compare_proc.communicate('\n'.join(inputs))
    
    # Check if matched.csv was created
    if not os.path.exists('data/output/matched.csv'):
        print("Error: Comparison failed to produce matched.csv")
        return
    
    # Run verification if API key is provided
    if args.api_key:
        print("Running verification on matched data")
        subprocess.run([
            'python', 'verify_pixel_spacing.py',
            '--csv', 'data/output/matched.csv',
            '--output', 'data/output/confirmed_matches.csv',
            '--api-key', args.api_key
        ], env=env)
        
        # Check if confirmed_matches.csv was created
        if os.path.exists('data/output/confirmed_matches.csv'):
            print("Verification completed successfully")
        else:
            print("Error: Verification failed to produce confirmed_matches.csv")
    else:
        print("Skipping verification step (no API key provided)")
    
    # List output files
    print("Output files:")
    for filename in os.listdir('data/output'):
        file_path = os.path.join('data/output', filename)
        size = os.path.getsize(file_path)
        print(f"  {filename} ({size} bytes)")

if __name__ == "__main__":
    main() 