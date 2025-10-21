"""MorphoSource specimen data comparison tool.

This module provides functionality to compare local specimen metadata
with MorphoSource database records, using fuzzy matching on catalog
numbers and taxonomic information.
"""
import ast
import json
import os
import re
from datetime import datetime, timedelta

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from fuzzywuzzy import fuzz, process
from IPython.display import HTML, display
from ipywidgets import widgets

# Define the MorphosourceMatcher class
class MorphosourceMatcher:
    def __init__(self, morphosource_data=None, comparison_data=None):
        """Initialize the matcher with two optional datasets."""
        self.morphosource_data = morphosource_data
        self.comparison_data = comparison_data
        self.matches = []
        self.match_scores = []
        self.threshold = 80  # Default threshold for fuzzy matching
    
    def load_morphosource_data(self, file_path):
        """Load Morphosource data from a JSON file."""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # Convert to DataFrame
            self.morphosource_data = pd.DataFrame(data)
            
            # Convert metadata from string to dict if needed
            if 'metadata' in self.morphosource_data.columns and isinstance(self.morphosource_data['metadata'].iloc[0], str):
                self.morphosource_data['metadata'] = self.morphosource_data['metadata'].apply(
                    lambda x: ast.literal_eval(x) if isinstance(x, str) else x
                )
            
            # Extract taxonomy from metadata for easier matching
            if 'metadata' in self.morphosource_data.columns:
                self.morphosource_data['taxonomy'] = self.morphosource_data['metadata'].apply(
                    lambda x: x.get('Taxonomy', '') if isinstance(x, dict) else ''
                )
                self.morphosource_data['object_id'] = self.morphosource_data['metadata'].apply(
                    lambda x: x.get('Object', '') if isinstance(x, dict) else ''
                )
                self.morphosource_data['element'] = self.morphosource_data['metadata'].apply(
                    lambda x: x.get('Element or Part', '') if isinstance(x, dict) else ''
                )
            
            print(f"Morphosource data loaded with {len(self.morphosource_data)} records")
            return True
        except Exception as e:
            print(f"Error loading Morphosource data: {str(e)}")
            return False
    
    def load_comparison_data(self, file_path, sheet_name=0):
        """Load comparison data from a CSV or Excel file."""
        try:
            if file_path.endswith('.csv'):
                # Try different CSV parsing options to handle potential delimiter issues
                try:
                    # First, try standard CSV parsing
                    self.comparison_data = pd.read_csv(file_path)
                    
                    # Check if all data got loaded into a single column
                    if len(self.comparison_data.columns) == 1 and 'Table 1' in self.comparison_data.columns:
                        # Try with tab delimiter
                        self.comparison_data = pd.read_csv(file_path, sep='\t')
                        
                        # If still problematic, try with different encoding
                        if len(self.comparison_data.columns) == 1:
                            self.comparison_data = pd.read_csv(file_path, sep='\t', encoding='latin1')
                            
                        # If still problematic, try to infer the separator
                        if len(self.comparison_data.columns) == 1:
                            from io import StringIO
                            with open(file_path, 'r', encoding='utf-8') as f:
                                sample = f.read(5000)  # Read a sample to detect delimiter
                            sniffer = pd.read_csv(StringIO(sample), sep=None, engine='python')
                            self.comparison_data = pd.read_csv(file_path, sep=None, engine='python')
                except Exception as csv_error:
                    print(f"Initial CSV parsing failed, trying alternative methods: {str(csv_error)}")
                    # Try with tab delimiter
                    self.comparison_data = pd.read_csv(file_path, sep='\t')
            elif file_path.endswith('.xlsx') or file_path.endswith('.xls'):
                self.comparison_data = pd.read_excel(file_path, sheet_name=sheet_name)
            else:
                # Try to parse as JSON
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.comparison_data = pd.DataFrame(data)
            
            # Clean column names by stripping whitespace
            self.comparison_data.columns = self.comparison_data.columns.str.strip()
            
            print(f"Comparison data loaded with {len(self.comparison_data)} records")
            print(f"Number of columns in comparison data: {len(self.comparison_data.columns)}")
            return True
        except Exception as e:
            print(f"Error loading comparison data: {str(e)}")
            return False
    
    def explore_morphosource_data(self):
        """Explore the Morphosource dataset."""
        if self.morphosource_data is None:
            print("Morphosource data is not loaded.")
            return
        
        print("Morphosource Data Overview:")
        print(f"Shape: {self.morphosource_data.shape}")
        print("\nColumn Names:")
        for col in self.morphosource_data.columns:
            print(f"- {col}")
        
        print("\nSample Data:")
        display(self.morphosource_data.head(3))
        
        # Show taxonomy distribution
        if 'taxonomy' in self.morphosource_data.columns:
            print("\nTop 10 Taxonomies:")
            display(self.morphosource_data['taxonomy'].value_counts().head(10))
    
    def explore_comparison_data(self):
        """Explore the comparison dataset."""
        if self.comparison_data is None:
            print("Comparison data is not loaded.")
            return
        
        print("Comparison Data Overview:")
        print(f"Shape: {self.comparison_data.shape}")
        print("\nColumn Names:")
        for col in self.comparison_data.columns:
            print(f"- {col}")
        
        print("\nSample Data:")
        display(self.comparison_data.head(3))
        
        # Show taxonomy distribution if available
        taxonomy_cols = [col for col in self.comparison_data.columns if 'taxonomy' in col.lower() or 'species' in col.lower()]
        if taxonomy_cols:
            print(f"\nTop 10 values in {taxonomy_cols[0]}:")
            display(self.comparison_data[taxonomy_cols[0]].value_counts().head(10))
    
    def normalize_catalog_number(self, catalog_str):
        """
        Normalizes catalog numbers by extracting consistent information from different formats.
        
        Examples:
        - "UF:Herp:14628-1" -> "UF:14628"  (keeps institution code)
        - "UF90369.pca" -> "UF:90369"
        - "UF-herps-68567-body.pca" -> "UF:68567"
        - "UF-H-165490-head.pca" -> "UF:165490"
        - "MCZ:Herp:4291" -> "MCZ:4291"
        - "AMNH:1234" -> "AMNH:1234"
        
        Returns a tuple of (normalized_catalog_number, institution_code, catalog_number_only)
        """
        if not catalog_str or not isinstance(catalog_str, str):
            return "", "", ""
        
        # Convert to uppercase for consistent processing
        catalog_str = catalog_str.upper()
        
        # Remove file extensions and common suffixes
        catalog_str = re.sub(r'\.PCA$', '', catalog_str)
        catalog_str = re.sub(r'[-_](HEAD|BODY|SKULL|SKELETON)$', '', catalog_str, flags=re.IGNORECASE)
        
        # Extract institution code using common patterns
        institution_match = re.match(r'^([A-Z]+)[-:_]', catalog_str)
        institution_code = ""
        
        if institution_match:
            # Institution code from prefix like "UF:" or "MCZ-"
            institution_code = institution_match.group(1)
        elif re.match(r'^[A-Z]{2,5}\d', catalog_str):
            # Institution code from fused format like "UF12345"
            institution_code = re.match(r'^([A-Z]{2,5})\d', catalog_str).group(1)
        
        # Find all numbers in the string
        numbers = re.findall(r'(\d+(?:-\d+)*)', catalog_str)
        catalog_number_only = ""
        
        if numbers:
            # Take the longest numeric match as the main catalog number
            main_number = max(numbers, key=len)
            
            # If the number has a hyphen, take just the first part as the base catalog number
            if '-' in main_number:
                catalog_number_only = main_number.split('-')[0]
            else:
                catalog_number_only = main_number
        
        # Create normalized format with institution code
        if institution_code and catalog_number_only:
            normalized = f"{institution_code}:{catalog_number_only}"
        elif catalog_number_only:
            normalized = catalog_number_only
        else:
            normalized = ""
            
        return normalized, institution_code, catalog_number_only

    def find_hierarchical_matches(self):
        """
        A taxonomically-focused hierarchical matching approach that:
        1. First groups records by taxonomic compatibility
        2. Then matches by catalog numbers within taxonomically compatible groups
        
        This method replaces the scoring-based approach with a more biologically meaningful process.
        """
        if self.morphosource_data is None or self.comparison_data is None:
            print("Both datasets must be loaded before finding matches.")
            return
        
        print("Beginning hierarchical taxonomic matching process...")
        start_time = datetime.now()
        
        # Create log file for match details
        log_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_filename = f"match_log_{log_timestamp}.txt"
        with open(log_filename, 'w') as log_file:
            log_file.write(f"=== Matching Process Started: {start_time} ===\n\n")
        
        # Initialize indexes for faster lookups
        print("Building indexes for fast matching...")
        index_start_time = datetime.now()
        
        # Index comparison data by taxonomic fields
        taxonomic_indexes = {}
        catalog_indexes = {}
        
        # Extract taxonomy fields from comparison data
        taxonomy_fields = []
        for field in self.comparison_data.columns:
            if any(term in field.lower() for term in ['family', 'genus', 'species', 'order', 'class', 'phylum']):
                taxonomy_fields.append(field)
        
        print(f"Found taxonomy fields in comparison data: {taxonomy_fields}")
        
        # Build indexes for each taxonomy level
        for field in taxonomy_fields:
            taxonomic_indexes[field] = {}
            # Get unique non-empty values
            unique_values = 0
            for idx, value in self.comparison_data[field].items():
                if pd.notna(value) and str(value).strip():
                    clean_value = str(value).lower().strip()
                    if clean_value not in taxonomic_indexes[field]:
                        taxonomic_indexes[field][clean_value] = []
                        unique_values += 1
                    taxonomic_indexes[field][clean_value].append(idx)
            print(f"Field '{field}' has {unique_values} unique values")
        
        # Index comparison data by catalog number
        catalog_indexes['full'] = {}
        catalog_indexes['numeric'] = {}
        catalog_indexes['institution'] = {}
        
        for idx, row in self.comparison_data.iterrows():
            if 'catalog_number' in row and pd.notna(row['catalog_number']):
                catalog = str(row['catalog_number']).strip()
                
                # Index the full catalog
                if catalog not in catalog_indexes['full']:
                    catalog_indexes['full'][catalog] = []
                catalog_indexes['full'][catalog].append(idx)
                
                # Extract institution code if present
                institution = ""
                if ':' in catalog:
                    parts = catalog.split(':')
                    institution = parts[0].strip().upper()
                    catalog_digits = parts[-1].strip()
                else:
                    catalog_digits = catalog
                    
                    # Try to extract institution from prefix if format is like "UF12345"
                    institution_match = re.match(r'^([A-Z]{2,5})\d', catalog)
                    if institution_match:
                        institution = institution_match.group(1)
                
                # Index by institution
                if institution:
                    if institution not in catalog_indexes['institution']:
                        catalog_indexes['institution'][institution] = []
                    catalog_indexes['institution'][institution].append(idx)
                
                # Index by numeric part
                catalog_numeric = re.sub(r'[^0-9]', '', catalog_digits)
                if catalog_numeric:
                    if catalog_numeric not in catalog_indexes['numeric']:
                        catalog_indexes['numeric'][catalog_numeric] = []
                    catalog_indexes['numeric'][catalog_numeric].append(idx)
        
        index_duration = (datetime.now() - index_start_time).total_seconds()
        print(f"Built indexes in {index_duration:.1f} seconds:")
        print(f"  - {sum(len(v) for field in taxonomic_indexes for v in taxonomic_indexes[field].values())} taxonomy index entries")
        print(f"  - {sum(len(idx) for idx_type in catalog_indexes for idx in catalog_indexes[idx_type].values())} catalog index entries")
        
        # Final matches will be stored here
        all_matches = []
        
        # Define batch size for processing
        batch_size = 1000
        total_morpho_records = len(self.morphosource_data)
        matches_by_type = {
            "genus_species": 0,
            "family": 0,
            "catalog_taxonomic": 0,
            "catalog_only": 0
        }
        
        # Process Morphosource records in batches to conserve memory
        for batch_start in range(0, total_morpho_records, batch_size):
            batch_end = min(batch_start + batch_size, total_morpho_records)
            batch_start_time = datetime.now()
            print(f"Processing Morphosource records {batch_start} to {batch_end} of {total_morpho_records}...")
            
            batch_matches = 0
            processed_records = 0
            
            for morpho_idx in range(batch_start, batch_end):
                processed_records += 1
                morpho_row = self.morphosource_data.iloc[morpho_idx]
                
                # Skip records without needed data
                if 'taxonomy' not in morpho_row or not morpho_row['taxonomy']:
                    continue
                    
                morpho_taxonomy = str(morpho_row['taxonomy']).lower().strip()
                
                # Get catalog information
                morpho_catalog = ""
                morpho_institution = ""
                morpho_catalog_numeric = ""
                
                if 'object_id' in morpho_row and morpho_row['object_id']:
                    obj_id = str(morpho_row['object_id']).strip()
                    
                    # Get institution code
                    if ':' in obj_id:
                        parts = obj_id.split(':')
                        morpho_institution = parts[0].strip().upper()
                        morpho_catalog = parts[-1].strip()
                    else:
                        morpho_catalog = obj_id
                        # Try to extract institution from prefix if format is like "UF12345"
                        institution_match = re.match(r'^([A-Z]{2,5})\d', obj_id)
                        if institution_match:
                            morpho_institution = institution_match.group(1)
                    
                    # Extract numeric part
                    morpho_catalog_numeric = re.sub(r'[^0-9]', '', morpho_catalog)
                
                # Find potential matches by taxonomic similarity first
                potential_matches = set()
                
                # Extract genus and species from morpho taxonomy
                genus_match = re.search(r'\b([A-Za-z][a-z]+)\b', morpho_taxonomy)
                morpho_genus = None
                if genus_match:
                    morpho_genus = genus_match.group(1).lower()
                
                # Look for matching genus in comparison data
                if morpho_genus:
                    # Check genus fields in comparison data
                    for field in taxonomy_fields:
                        if 'genus' in field.lower() and morpho_genus in taxonomic_indexes[field]:
                            potential_matches.update(taxonomic_indexes[field][morpho_genus])
                
                # Look for matching family in comparison data
                morpho_family = next((word for word in re.findall(r'\b[a-z]+\b', morpho_taxonomy) 
                                   if word.endswith('idae')), None)
                if morpho_family:
                    for field in taxonomy_fields:
                        if 'family' in field.lower() and morpho_family in taxonomic_indexes[field]:
                            potential_matches.update(taxonomic_indexes[field][morpho_family])
                
                # If no matches by taxonomy, try catalog-based matching
                if not potential_matches and morpho_catalog_numeric:
                    # Try matching by numeric part of catalog
                    if morpho_catalog_numeric in catalog_indexes['numeric']:
                        catalog_candidates = catalog_indexes['numeric'][morpho_catalog_numeric]
                        
                        # Filter by institution if available
                        if morpho_institution and morpho_institution in catalog_indexes['institution']:
                            institution_candidates = set(catalog_indexes['institution'][morpho_institution])
                            catalog_candidates = [c for c in catalog_candidates if c in institution_candidates]
                        
                        # Check these candidates for taxonomic compatibility
                        for comp_idx in catalog_candidates:
                            comp_row = self.comparison_data.iloc[comp_idx]
                            
                            # Build taxonomy for comparison record
                            comp_taxonomy_parts = []
                            for field in taxonomy_fields:
                                if pd.notna(comp_row[field]) and str(comp_row[field]).strip():
                                    comp_taxonomy_parts.append(str(comp_row[field]).lower())
                            
                            comp_taxonomy = ' '.join(comp_taxonomy_parts)
                            
                            # Check taxonomic compatibility
                            compatibility = self.check_taxonomic_compatibility(morpho_taxonomy, comp_taxonomy)
                            
                            # Only include if not strongly incompatible
                            if compatibility > -2:
                                potential_matches.add(comp_idx)
                
                # Evaluate each potential match
                best_match = None
                best_score = 0
                best_reason = ""
                best_match_type = ""
                
                for comp_idx in potential_matches:
                    comp_row = self.comparison_data.iloc[comp_idx]
                    
                    # Build taxonomy string from comparison record
                    comp_taxonomy_parts = []
                    for field in taxonomy_fields:
                        if pd.notna(comp_row[field]) and str(comp_row[field]).strip():
                            comp_taxonomy_parts.append(str(comp_row[field]).lower())
                    
                    comp_taxonomy = ' '.join(comp_taxonomy_parts)
                    
                    # Determine taxonomic compatibility
                    compatibility = self.check_taxonomic_compatibility(morpho_taxonomy, comp_taxonomy)
                    
                    # Skip strongly incompatible matches
                    if compatibility == -2:
                        continue
                    
                    # Get catalog information from comparison record
                    comp_catalog = ""
                    comp_institution = ""
                    comp_catalog_numeric = ""
                    
                    if 'catalog_number' in comp_row and pd.notna(comp_row['catalog_number']):
                        catalog = str(comp_row['catalog_number']).strip()
                        
                        if ':' in catalog:
                            parts = catalog.split(':')
                            comp_institution = parts[0].strip().upper()
                            comp_catalog = parts[-1].strip()
                        else:
                            comp_catalog = catalog
                            # Try to extract institution
                            institution_match = re.match(r'^([A-Z]{2,5})\d', catalog)
                            if institution_match:
                                comp_institution = institution_match.group(1)
                        
                        comp_catalog_numeric = re.sub(r'[^0-9]', '', comp_catalog)
                    
                    # Calculate match score and reason
                    score = 0
                    reason = ""
                    match_type = ""
                    
                    # Strong taxonomic compatibility (same genus/species)
                    if compatibility == 2:
                        score = 90
                        reason = "Same genus/species"
                        match_type = "genus_species"
                        
                        # Exact catalog match
                        if morpho_catalog_numeric and comp_catalog_numeric and morpho_catalog_numeric == comp_catalog_numeric:
                            score = 100
                            reason += " + exact catalog match"
                            
                            # Same institution too
                            if morpho_institution and comp_institution and morpho_institution == comp_institution:
                                score += 5
                                reason += " + same institution"
                    
                    # Moderate taxonomic compatibility (same family)
                    elif compatibility == 1:
                        score = 70
                        reason = "Same family"
                        match_type = "family"
                        
                        # Exact catalog match
                        if morpho_catalog_numeric and comp_catalog_numeric and morpho_catalog_numeric == comp_catalog_numeric:
                            score = 85
                            reason += " + exact catalog match"
                            
                            # Same institution too
                            if morpho_institution and comp_institution and morpho_institution == comp_institution:
                                score += 5
                                reason += " + same institution"
                    
                    # Unknown compatibility but not incompatible
                    elif compatibility == 0:
                        # Remove this matching option - require positive taxonomic compatibility
                        pass
                    
                    # Moderate incompatibility (same major group)
                    elif compatibility == -1:
                        # Remove this matching option - require positive taxonomic compatibility
                        pass
                    
                    # Strong incompatibility
                    elif compatibility == -2:
                        # Never match taxonomically incompatible records
                        pass
                    
                    # Update best match if we found a better one
                    if score > best_score:
                        best_score = score
                        best_match = (comp_idx, comp_row)
                        best_reason = reason
                        best_match_type = match_type
                
                # If we found a match with a reasonable score, add it
                if best_match and best_score >= 50:
                    comp_idx, comp_row = best_match
                    
                    # Get taxonomic info for display
                    comp_taxonomy_parts = []
                    for field in taxonomy_fields:
                        if pd.notna(comp_row[field]) and str(comp_row[field]).strip():
                            comp_taxonomy_parts.append(f"{field}: {comp_row[field]}")
                    
                    comp_taxonomy = '; '.join(comp_taxonomy_parts)
                    
                    # Create match record
                    match_record = (
                        morpho_idx,
                        morpho_row,
                        comp_idx,
                        comp_row,
                        best_score,
                        morpho_taxonomy,
                        f"{best_reason} | Morphosource: {morpho_taxonomy} | Comparison: {comp_taxonomy}"
                    )
                    
                    # Add to matches
                    all_matches.append(match_record)
                    batch_matches += 1
                    
                    # Update match type counters
                    if best_match_type:
                        matches_by_type[best_match_type] = matches_by_type.get(best_match_type, 0) + 1
                    
                    # Log the match to file
                    with open(log_filename, 'a') as log_file:
                        log_file.write(f"MATCH {len(all_matches)}: Score {best_score}\n")
                        log_file.write(f"  Morphosource: {morpho_row.get('title', '')[:50]}... | Taxonomy: {morpho_taxonomy}\n")
                        log_file.write(f"  Comparison: {comp_taxonomy}\n")
                        log_file.write(f"  Reason: {best_reason}\n")
                        log_file.write("\n")
                
                # Give real-time progress updates every 100 records
                if processed_records % 100 == 0:
                    current_time = datetime.now()
                    elapsed = (current_time - batch_start_time).total_seconds()
                    records_per_second = processed_records / max(elapsed, 0.001)
                    
                    progress_pct = (batch_start + processed_records) / total_morpho_records * 100
                    print(f"  Progress: {batch_start + processed_records}/{total_morpho_records} ({progress_pct:.1f}%) - "
                          f"Speed: {records_per_second:.1f} records/sec - Found {batch_matches} matches in current batch")
            
            # Status update after each batch
            batch_duration = (datetime.now() - batch_start_time).total_seconds()
            matches_so_far = len(all_matches)
            percent_done = (batch_end / total_morpho_records) * 100
            
            # Calculate estimated time remaining
            elapsed_total = (datetime.now() - start_time).total_seconds()
            records_processed = batch_end
            records_remaining = total_morpho_records - records_processed
            
            if records_processed > 0:
                seconds_per_record = elapsed_total / records_processed
                est_remaining_seconds = records_remaining * seconds_per_record
                est_remaining = str(timedelta(seconds=int(est_remaining_seconds)))
            else:
                est_remaining = "Unknown"
                
            print(f"Processed {batch_end}/{total_morpho_records} records ({percent_done:.1f}%) - "
                  f"Found {matches_so_far} matches so far - Batch took {batch_duration:.1f} sec - "
                  f"Est. remaining: {est_remaining}")
            
            # Log batch summary
            with open(log_filename, 'a') as log_file:
                log_file.write(f"Batch {batch_start}-{batch_end} completed in {batch_duration:.1f} seconds\n")
                log_file.write(f"  Found {batch_matches} matches in this batch\n")
                log_file.write(f"  Total matches so far: {matches_so_far}\n\n")
        
        self.matches = all_matches
        self.match_scores = [match[4] for match in all_matches]
        
        end_time = datetime.now()
        elapsed = (end_time - start_time).total_seconds()
        
        # Final summary
        print(f"\nMatching completed in {elapsed:.1f} seconds")
        print(f"Found {len(all_matches)} matches in total:")
        
        # Summarize match quality
        score_bins = {
            "95-100": 0,
            "90-94": 0,
            "80-89": 0,
            "70-79": 0, 
            "60-69": 0,
            "50-59": 0
        }
        
        for _, _, _, _, score, _, _ in all_matches:
            if score >= 95:
                score_bins["95-100"] += 1
            elif score >= 90:
                score_bins["90-94"] += 1
            elif score >= 80:
                score_bins["80-89"] += 1
            elif score >= 70:
                score_bins["70-79"] += 1
            elif score >= 60:
                score_bins["60-69"] += 1
            else:
                score_bins["50-59"] += 1
        
        print("\nMatch quality summary:")
        for bin_name, count in score_bins.items():
            if count > 0:
                percent = (count / len(all_matches)) * 100
                print(f"  - {bin_name} points: {count} matches ({percent:.1f}%)")
        
        print("\nMatch type summary:")
        for match_type, count in matches_by_type.items():
            if count > 0:
                percent = (count / len(all_matches)) * 100
                print(f"  - {match_type}: {count} matches ({percent:.1f}%)")
        
        # Write final summary to log
        with open(log_filename, 'a') as log_file:
            log_file.write(f"\n=== Matching Process Completed: {end_time} ===\n")
            log_file.write(f"Total runtime: {elapsed:.1f} seconds\n")
            log_file.write(f"Found {len(all_matches)} matches\n\n")
            
            log_file.write("Match quality summary:\n")
            for bin_name, count in score_bins.items():
                if count > 0:
                    percent = (count / len(all_matches)) * 100
                    log_file.write(f"  - {bin_name} points: {count} matches ({percent:.1f}%)\n")
            
            log_file.write("\nMatch type summary:\n")
            for match_type, count in matches_by_type.items():
                if count > 0:
                    percent = (count / len(all_matches)) * 100
                    log_file.write(f"  - {match_type}: {count} matches ({percent:.1f}%)\n")
        
        print(f"\nDetailed match log has been saved to: {log_filename}")
        return all_matches

    def display_matches(self, limit=10):
        """Display the matches found."""
        if not self.matches:
            print("No matches found. Run find_matches_by_taxonomy or find_matches_by_catalog_number first.")
            return
        
        print(f"Displaying top {min(limit, len(self.matches))} matches:")
        
        for i, (idx1, row1, idx2, row2, score, field1, field2) in enumerate(self.matches[:limit]):
            print(f"\nMatch #{i+1} (Score: {score})")
            
            # Extract catalog numbers for clearer display
            morpho_catalog = ""
            if 'object_id' in row1 and pd.notna(row1['object_id']):
                obj_id = str(row1['object_id'])
                if ':' in obj_id:
                    morpho_catalog = obj_id.split(':')[-1]
            
            comp_catalog = ""
            if 'catalog_number' in row2 and pd.notna(row2['catalog_number']):
                comp_catalog = str(row2['catalog_number'])
            
            # Determine match type for display
            match_type = ""
            if morpho_catalog and comp_catalog:
                if morpho_catalog == comp_catalog:
                    match_type = "✅ CATALOG NUMBER MATCH"
                else:
                    match_type = "⚠️ CATALOG NUMBER MISMATCH"
            
            print("Morphosource Record:")
            display_dict = {
                'title': row1['title'],
                'id': row1['id'],
                'taxonomy': row1.get('taxonomy', ''),
                'object_id': row1.get('object_id', ''),
                'catalog_number': morpho_catalog,  # Add extracted catalog number
                'element': row1.get('element', ''),
                'url': row1['url']
            }
            display(pd.DataFrame([display_dict]).T)
            
            print("Comparison Record:")
            # Display only the most relevant columns
            relevant_cols = []
            for col in row2.index:
                if pd.notna(row2[col]) and any(term in col.lower() for term in 
                                              ['genus', 'species', 'family', 'catalog', 
                                               'specimen', 'id', 'number', 'taxonomy']):
                    relevant_cols.append(col)
            
            display(pd.DataFrame([row2[relevant_cols]]).T)
            
            print(f"Matched: '{field1}' with '{field2}'")
            
            # Show catalog number comparison
            if morpho_catalog or comp_catalog:
                print(f"Catalog Numbers: Morphosource={morpho_catalog} | Comparison={comp_catalog}")
                if match_type:
                    print(match_type)
                    
    def export_matches_to_csv(self, output_file="matched.csv"):
        """
        Export matches to CSV file, adding three new columns to the comparison data:
        1. Match_Found (yes/no) - Indicates if a match was found
        2. Morphosource_URL - URL to the matched resource on Morphosource
        3. Match_Score - Numerical score of the match quality
        
        Args:
            output_file (str): Path to the output CSV file
        """
        if self.comparison_data is None:
            print("No comparison data loaded. Nothing to export.")
            return False
        
        print(f"Exporting matches to {output_file}...")
        
        # Create a copy of the comparison data
        export_df = self.comparison_data.copy()
        
        # Add the three new columns with default values (no match)
        export_df['Match_Found'] = 'no'
        export_df['Morphosource_URL'] = ''
        export_df['Match_Score'] = 0
        
        # Dictionary to count matches by catalog number
        matches_by_catalog = {}
        
        # If there are matches, update the corresponding rows
        if self.matches:
            # Get indices of matches in the comparison data
            match_indices = {}
            for idx1, row1, idx2, row2, score, field1, field2 in self.matches:
                # Each comparison record could match multiple Morphosource records
                if idx2 in match_indices:
                    # If the same comparison record has multiple matches, keep the one with highest score
                    if score > match_indices[idx2]['score']:
                        match_indices[idx2] = {
                            'morpho_row': row1,
                            'score': score,
                            'details': field2
                        }
                else:
                    match_indices[idx2] = {
                        'morpho_row': row1,
                        'score': score,
                        'details': field2
                    }
                    
                # Track match counts by catalog number
                catalog_num = str(row2['catalog_number']) if 'catalog_number' in row2 else 'unknown'
                if catalog_num not in matches_by_catalog:
                    matches_by_catalog[catalog_num] = 0
                matches_by_catalog[catalog_num] += 1
            
            # Update the export dataframe with match information
            for idx2, match_info in match_indices.items():
                export_df.loc[idx2, 'Match_Found'] = 'yes'
                
                # Set the Morphosource URL if available
                if 'url' in match_info['morpho_row'] and pd.notna(match_info['morpho_row']['url']):
                    export_df.loc[idx2, 'Morphosource_URL'] = match_info['morpho_row']['url']
                
                # Set the match score
                export_df.loc[idx2, 'Match_Score'] = match_info['score']
        
        # Save to CSV
        try:
            export_df.to_csv(output_file, index=False)
            
            # Success report
            match_count = len([x for x in export_df['Match_Found'] if x == 'yes'])
            total_count = len(export_df)
            match_percent = (match_count / total_count) * 100 if total_count > 0 else 0
            
            print(f"Successfully exported {match_count} matches out of {total_count} records ({match_percent:.1f}%).")
            
            # Report catalog numbers with multiple matches
            multiple_matches = {k: v for k, v in matches_by_catalog.items() if v > 1}
            if multiple_matches:
                print(f"\nFound {len(multiple_matches)} catalog numbers with multiple matches:")
                for catalog, count in sorted(multiple_matches.items(), key=lambda x: x[1], reverse=True)[:10]:
                    print(f"  - Catalog {catalog}: {count} matches")
                if len(multiple_matches) > 10:
                    print(f"  - ... and {len(multiple_matches) - 10} more")
            
            return True
        except Exception as e:
            print(f"Error saving CSV file: {str(e)}")
            return False
    
    def interactive_match_review(self):
        """Interactive widget to review and confirm matches."""
        if not self.matches:
            print("No matches found. Run find_matches_by_taxonomy or find_matches_by_catalog_number first.")
            return
        
        # Create a DataFrame for easier manipulation
        match_df = pd.DataFrame({
            'idx1': [m[0] for m in self.matches],
            'idx2': [m[2] for m in self.matches],
            'score': [m[4] for m in self.matches],
            'field1': [m[5] for m in self.matches],
            'field2': [m[6] for m in self.matches],
            'confirmed': [False] * len(self.matches)
        })
        
        # Sort by score descending
        match_df = match_df.sort_values('score', ascending=False).reset_index(drop=True)
        
        # Current match index
        current_idx = [0]
        
        # Display widgets
        match_info = widgets.Output()
        
        def show_match(idx):
            with match_info:
                match_info.clear_output()
                
                if idx < 0 or idx >= len(match_df):
                    print("No more matches to review.")
                    return
                
                row = match_df.iloc[idx]
                record1 = self.morphosource_data.iloc[row['idx1']]
                record2 = self.comparison_data.iloc[row['idx2']]
                
                print(f"Match #{idx+1}/{len(match_df)} (Score: {row['score']})")
                
                print("\nMorphosource Record:")
                display_dict = {
                    'title': record1['title'],
                    'id': record1['id'],
                    'taxonomy': record1.get('taxonomy', ''),
                    'object_id': record1.get('object_id', ''),
                    'element': record1.get('element', ''),
                    'url': record1['url']
                }
                display(pd.DataFrame([display_dict]).T)
                
                print("\nComparison Record:")
                # Display only the most relevant columns
                relevant_cols = []
                for col in record2.index:
                    if pd.notna(record2[col]) and any(term in col.lower() for term in 
                                                  ['genus', 'species', 'family', 'catalog', 
                                                   'specimen', 'id', 'number', 'taxonomy']):
                        relevant_cols.append(col)
                
                display(pd.DataFrame([record2[relevant_cols]]).T)
                
                print(f"\nMatched: '{row['field1']}' with '{row['field2']}'")
                
                status = "✅ Confirmed" if row['confirmed'] else "❌ Not Confirmed"
                print(f"\nStatus: {status}")
        
        # Button callbacks
        def on_confirm(b):
            idx = current_idx[0]
            if idx < len(match_df):
                match_df.at[idx, 'confirmed'] = True
                current_idx[0] += 1
                show_match(current_idx[0])
        
        def on_reject(b):
            idx = current_idx[0]
            if idx < len(match_df):
                match_df.at[idx, 'confirmed'] = False
                current_idx[0] += 1
                show_match(current_idx[0])
        
        def on_prev(b):
            if current_idx[0] > 0:
                current_idx[0] -= 1
                show_match(current_idx[0])
        
        def on_next(b):
            if current_idx[0] < len(match_df) - 1:
                current_idx[0] += 1
                show_match(current_idx[0])
        
        def on_export(b):
            confirmed_matches = match_df[match_df['confirmed']].copy()
            if len(confirmed_matches) > 0:
                # Create a more detailed DataFrame with the actual record data
                result = []
                for _, row in confirmed_matches.iterrows():
                    morpho_record = self.morphosource_data.iloc[row['idx1']]
                    comp_record = self.comparison_data.iloc[row['idx2']]
                    
                    # Create a clean dictionary for the Morphosource record
                    morpho_dict = {
                        'title': morpho_record['title'],
                        'id': morpho_record['id'],
                        'url': morpho_record['url'],
                        'taxonomy': morpho_record.get('taxonomy', ''),
                        'object_id': morpho_record.get('object_id', ''),
                        'element': morpho_record.get('element', '')
                    }
                    
                    result.append({
                        'score': row['score'],
                        'morphosource_record': morpho_dict,
                        'comparison_record': comp_record.to_dict(),
                        'matched_field1': row['field1'],
                        'matched_field2': row['field2']
                    })
                
                # Export to CSV and JSON
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                
                # Export as JSON
                with open(f'confirmed_matches_{timestamp}.json', 'w', encoding='utf-8') as f:
                    json.dump(result, f, indent=2)
                
                # Also create a flattened version for CSV
                flat_result = []
                for item in result:
                    flat_item = {
                        'match_score': item['score'],
                        'matched_field1': item['matched_field1'],
                        'matched_field2': item['matched_field2']
                    }
                    
                    # Add morphosource fields with prefix
                    for k, v in item['morphosource_record'].items():
                        flat_item[f'morpho_{k}'] = v
                        
                    # Add comparison fields with prefix
                    for k, v in item['comparison_record'].items():
                        flat_item[f'comp_{k}'] = v
                    
                    flat_result.append(flat_item)
                
                # Export as CSV
                pd.DataFrame(flat_result).to_csv(f'confirmed_matches_{timestamp}.csv', index=False)
                
                print(f"Exported {len(confirmed_matches)} confirmed matches to:")
                print(f"- confirmed_matches_{timestamp}.json")
                print(f"- confirmed_matches_{timestamp}.csv")
            else:
                print("No confirmed matches to export.")
        
        # Create buttons
        confirm_btn = widgets.Button(description="Confirm Match", button_style='success')
        reject_btn = widgets.Button(description="Reject Match", button_style='danger')
        prev_btn = widgets.Button(description="Previous")
        next_btn = widgets.Button(description="Next")
        export_btn = widgets.Button(description="Export Confirmed", button_style='info')
        
        confirm_btn.on_click(on_confirm)
        reject_btn.on_click(on_reject)
        prev_btn.on_click(on_prev)
        next_btn.on_click(on_next)
        export_btn.on_click(on_export)
        
        # Layout
        nav_buttons = widgets.HBox([prev_btn, next_btn])
        action_buttons = widgets.HBox([confirm_btn, reject_btn, export_btn])
        
        # Display everything
        display(nav_buttons)
        display(action_buttons)
        display(match_info)
        
        # Show the first match
        show_match(0)
        
        return match_df

    def export_invalid_records_to_csv(self, output_file="invalid.csv"):
        """
        Export records that have neither valid taxonomy nor valid catalog numbers to CSV file.
        These are the records that are skipped during the matching process.
        
        Args:
            output_file (str): Path to the output CSV file
        """
        if not hasattr(self, 'invalid_records') or self.invalid_records is None or len(self.invalid_records) == 0:
            print("No invalid records found to export.")
            return False
        
        print(f"Exporting {len(self.invalid_records)} invalid records to {output_file}...")
        
        try:
            self.invalid_records.to_csv(output_file, index=False)
            print(f"Successfully exported {len(self.invalid_records)} invalid records to {output_file}.")
            
            # Provide some basic statistics about the invalid records
            print("\nInvalid records summary:")
            
            # Check most common reasons for invalidity
            empty_taxonomy_count = self.invalid_records['taxonomy'].isna().sum() + (self.invalid_records['taxonomy'] == '').sum()
            empty_object_id_count = self.invalid_records['object_id'].isna().sum() + (self.invalid_records['object_id'] == '').sum()
            
            print(f"  - Records with empty taxonomy: {empty_taxonomy_count} ({empty_taxonomy_count/len(self.invalid_records)*100:.1f}%)")
            print(f"  - Records with empty object_id: {empty_object_id_count} ({empty_object_id_count/len(self.invalid_records)*100:.1f}%)")
            
            # Display sample of invalid records
            print("\nSample of invalid records (first 5):")
            pd.set_option('display.max_columns', None)
            print(self.invalid_records.head(5).to_string())
            pd.reset_option('display.max_columns')
            
            return True
        except Exception as e:
            print(f"Error saving invalid records to CSV file: {str(e)}")
            return False

    def check_taxonomic_compatibility(self, taxonomy1, taxonomy2):
        """
        Determines if two taxonomies are compatible or represent completely different groups.
        Returns:
        - 2: Strong compatibility (same species or genus)
        - 1: Moderate compatibility (same family)
        - 0: Unknown compatibility (not enough information)
        - -1: Moderate incompatibility (different families but same order/class)
        - -2: Strong incompatibility (completely different taxonomic groups)
        """
        # Create a cache key for this pair of taxonomies
        # Use a frozenset so order doesn't matter (compatibility is symmetric)
        cache_key = frozenset([taxonomy1, taxonomy2])
        
        # Check if we've already computed compatibility for these taxonomies
        if hasattr(self, '_compatibility_cache'):
            if cache_key in self._compatibility_cache:
                return self._compatibility_cache[cache_key]
        else:
            # Initialize the cache if it doesn't exist
            self._compatibility_cache = {}
            
        if not taxonomy1 or not taxonomy2:
            self._compatibility_cache[cache_key] = -2  # Changed from 0 to -2: assume incompatible if either taxonomy is missing
            return -2
            
        # Convert to lowercase for comparison
        tax1 = taxonomy1.lower()
        tax2 = taxonomy2.lower()
        
        # Define a set of human-related terms to identify human specimens
        human_terms = {'homo sapiens', 'human', 'homo', 'sapiens', 'hominid', 'hominidae'}
        
        # Check if one is human and the other is not
        is_tax1_human = any(term in tax1 for term in human_terms)
        is_tax2_human = any(term in tax2 for term in human_terms)
        
        if is_tax1_human != is_tax2_human:
            # One is human, one is not - strong incompatibility
            self._compatibility_cache[cache_key] = -2
            return -2
        
        # Extract genus and species from taxonomy strings
        # Most scientific names will be in format "Genus species" or similar variations
        genus1 = None
        species1 = None
        genus2 = None
        species2 = None
        
        # Extract genus from taxonomy1
        genus_match1 = re.search(r'\b([A-Za-z][a-z]+)\b', tax1)
        if genus_match1:
            genus1 = genus_match1.group(1).lower()
            
            # Look for species after genus
            species_match1 = re.search(rf'{re.escape(genus1)}\s+([a-z]+\b)', tax1)
            if species_match1:
                species1 = species_match1.group(1).lower()
        
        # Extract genus from taxonomy2
        genus_match2 = re.search(r'\b([A-Za-z][a-z]+)\b', tax2)
        if genus_match2:
            genus2 = genus_match2.group(1).lower()
            
            # Look for species after genus
            species_match2 = re.search(rf'{re.escape(genus2)}\s+([a-z]+\b)', tax2)
            if species_match2:
                species2 = species_match2.group(1).lower()
        
        # Check for exact genus and species match
        if genus1 and genus2 and genus1 == genus2:
            if species1 and species2 and species1 == species2:
                # Exact species match
                self._compatibility_cache[cache_key] = 2
                return 2
            
            # Same genus
            self._compatibility_cache[cache_key] = 2
            return 2
        
        # Split taxonomies into words for more general comparison
        words1 = set(re.findall(r'\b[a-z]+\b', tax1))
        words2 = set(re.findall(r'\b[a-z]+\b', tax2))
        
        # Extract family names (usually end in -idae for animals)
        family1 = next((word for word in words1 if word.endswith('idae')), None)
        family2 = next((word for word in words2 if word.endswith('idae')), None)
        
        # If different known families, that's an incompatibility
        if family1 and family2 and family1 != family2:
            self._compatibility_cache[cache_key] = -2  # Changed from no check to -2: different families means incompatible
            return -2
            
        # If same family, that's a good match
        if family1 and family2 and family1 == family2:
            self._compatibility_cache[cache_key] = 1
            return 1
            
        # Check how many words they share
        common_words = words1.intersection(words2)
        
        # If they share meaningful words (except common taxonomic terms), probably related
        if len(common_words) >= 2:
            self._compatibility_cache[cache_key] = 1
            return 1
            
        if len(common_words) == 1:
            # Check if the common word is a meaningful taxon and not just a common word
            common_word = next(iter(common_words))
            if (common_word.endswith('idae') or  # family
                common_word.endswith('inae') or  # subfamily
                common_word.endswith('ini') or   # tribe
                common_word.endswith('inae') or  # subfamily
                common_word.endswith('oidea') or # superfamily
                common_word.endswith('iformes')): # order names in birds, etc.
                self._compatibility_cache[cache_key] = 1
                return 1
            
        # Check for incompatible taxonomic groups
        # List of high-level taxonomic groups that should not match across boundaries
        taxonomic_classes = [
            {'reptilia', 'squamata', 'sauria', 'serpentes', 'testudines', 'crocodilia'},
            {'aves', 'neornithes'},
            {'mammalia', 'theria', 'eutheria', 'metatheria', 'homo', 'sapiens', 'hominid', 'hominidae'},
            {'amphibia', 'anura', 'caudata'},
            {'osteichthyes', 'actinopterygii', 'chondrichthyes'},
            {'dinosauria', 'ornithischia', 'saurischia'},
            {'arthropoda', 'insecta', 'arachnida', 'crustacea'},
            {'mollusca', 'cephalopoda', 'gastropoda', 'bivalvia'}
        ]
        
        # Find which group each taxonomy belongs to
        group1 = None
        group2 = None
        
        for i, group in enumerate(taxonomic_classes):
            if any(term in words1 for term in group):
                group1 = i
            if any(term in words2 for term in group):
                group2 = i
        
        # If they belong to different high-level groups
        if group1 is not None and group2 is not None and group1 != group2:
            self._compatibility_cache[cache_key] = -2
            return -2
        
        # If we've determined they're in the same group but don't share specific taxonomy
        if group1 is not None and group1 == group2:
            self._compatibility_cache[cache_key] = -1
            return -1
        
        # Default: insufficient information to determine compatibility
        # Changed from 0 to -2: assume incompatible unless we have positive evidence of compatibility
        self._compatibility_cache[cache_key] = -2
        return -2

# Main execution code
if __name__ == "__main__":
    import sys
    
    # Create a matcher instance
    matcher = MorphosourceMatcher()
    
    # Check if arguments are provided through command line
    if len(sys.argv) > 3:
        morphosource_file = sys.argv[1]
        comparison_file = sys.argv[2]
        output_file = sys.argv[3]
    else:
        # Use standard input for interactive mode
        morphosource_file = input("Enter path to Morphosource data JSON file: ")
        comparison_file = input("Enter path to comparison CSV file: ")
        output_file = input("Enter output CSV filename (default: matched.csv): ")
        output_file = output_file.strip() if output_file.strip() else "matched.csv"
    
    # Check if files exist
    if not os.path.exists(morphosource_file):
        print(f"Error: File not found: {morphosource_file}")
        sys.exit(1)
    else:
        # Load Morphosource data
        matcher.load_morphosource_data(morphosource_file)
        
        # Explore the dataset
        matcher.explore_morphosource_data()
    
    if not os.path.exists(comparison_file):
        print(f"Error: File not found: {comparison_file}")
        sys.exit(1)
    else:
        # Load comparison data
        matcher.load_comparison_data(comparison_file)
        
        # Explore the dataset
        matcher.explore_comparison_data()
    
    # If both datasets are loaded, proceed with matching
    if matcher.morphosource_data is not None and matcher.comparison_data is not None:
        print("\nPerforming hierarchical matching based on taxonomy first, then catalog numbers...")
        
        # Use the new hierarchical matching method
        matcher.find_hierarchical_matches()
        
        # Display matches
        if matcher.matches:
            # Export matches to CSV
            matcher.export_matches_to_csv(output_file=output_file)
            
            # Export invalid records to CSV
            if hasattr(matcher, 'invalid_records') and matcher.invalid_records is not None:
                invalid_file = output_file.replace('.csv', '_invalid.csv')
                matcher.export_invalid_records_to_csv(output_file=invalid_file)
            
            # Check if we should run interactive review
            if len(sys.argv) <= 4:
                review = input("Would you like to interactively review matches? (y/n): ")
                if review.lower() == 'y':
                    match_df = matcher.interactive_match_review()
