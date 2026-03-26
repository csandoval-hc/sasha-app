import pandas as pd
import sqlite3
import os

csv_filename = 'table1.csv'

if not os.path.exists(csv_filename):
    print(f"Error: {csv_filename} not found!")
else:
    print("Reading CSV... (Auto-detecting formatting)")
    
    try:
        # 'sep=None' and 'engine=python' forces pandas to guess the correct separator
        df = pd.read_csv(
            csv_filename, 
            nrows=1000, 
            encoding='latin1', 
            sep=None, 
            engine='python',
            on_bad_lines='skip'
        )
        
        conn = sqlite3.connect('sandbox.sqlite')
        df.to_sql('my_test_table', conn, index=False, if_exists='replace')
        
        print("--- SUCCESS! Created sandbox.sqlite ---")
        conn.close()
        
    except Exception as e:
        print(f"We hit a wall: {e}")