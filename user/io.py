# AUTOGLANCING 
# @mattwfranchi 

"""
Data input/output module for the AutoGlancing project.
Provides dataset classes and utilities for loading and processing Nexar datasets.
"""
from abc import ABC, abstractmethod
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
import os
import fire
import pandas as pd 
import sys
import traceback

from utils.logger import get_logger
from utils.timer import time_it

# Memory monitoring
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

# Add pandarallel for parallel DataFrame operations
try:
    from pandarallel import pandarallel
    PANDARALLEL_AVAILABLE = True
except ImportError:
    PANDARALLEL_AVAILABLE = False

# ===== Constants =====
NEXAR_2023 = "/share/ju/nexar_data/2023"
NEXAR_2020 = "/share/pierson/nexar_data/raw_data"
EXPORT_DIR = "/share/ju/autoglancing/data/raw_export"

try:
    import pyarrow
    import pyarrow.parquet as pq
    PARQUET_AVAILABLE = True
except ImportError:
    PARQUET_AVAILABLE = False

# Initialize pandarallel at module level if available
if PANDARALLEL_AVAILABLE:
    # Disable progress bars completely to avoid flooding logs
    pandarallel.initialize(progress_bar=False, verbose=0)


class BaseNexarDataset(ABC):
    """Abstract base class for Nexar datasets."""
    
    def __init__(self, load_imgs=False, load_md=False, ncpus=8, export=False):
        """Initialize the dataset.
        
        Args:
            load_imgs: If True, load images during initialization
            load_md: If True, load metadata during initialization
            ncpus: Number of CPUs to use for parallel processing
            export: If True, export data to files after loading
        """
        self.ncpus = ncpus
        self.export_flag = export
        self.logger = get_logger(self.__class__.__name__)
        
        self.imgs = []
        self.md = pd.DataFrame()
        
        if load_imgs:
            self.load_images(export=export)
        
        if load_md:
            self.load_metadata(export=export)
    
    @abstractmethod
    def load_images(self, export=False):
        """Load images from the dataset."""
        pass
    
    @abstractmethod
    def load_metadata(self, export=False):
        """Load metadata from the dataset."""
        pass
    
    def filter_by_date(self, start_date=None, end_date=None, export=False):
        """Filter the dataset by date range.
        
        Args:
            start_date: Start date as string (YYYY-MM-DD) or datetime
            end_date: End date as string (YYYY-MM-DD) or datetime
            export: Whether to export the filtered data
            
        Returns:
            Tuple of (filtered images, filtered metadata)
        """
        return filter_dataset_by_date(self, start_date, end_date, export)
    
    def filter_by_specific_dates(self, dates, export=False, format="parquet"):
        """Filter the dataset to include only records from specific dates.
        
        Args:
            dates: List of dates as strings (YYYY-MM-DD) or datetime objects
            export: Whether to export the filtered data
            format: Export format for metadata ('parquet' or 'csv')
            
        Returns:
            Tuple of (filtered_images, filtered_metadata)
        """
        return filter_dataset_by_specific_dates(self, dates, export, format)
    
    def export_statistics(self, output_file=None):
        """Export statistics about the dataset.
        
        Args:
            output_file: Path to output file, or None to print to console
        """
        return export_dataset_statistics(self, output_file)
    
    def _chunk_list(self, lst, num_chunks):
        """Split a list into roughly equal chunks.
        
        Args:
            lst: The list to split
            num_chunks: Number of chunks to create
            
        Returns:
            List of chunks (each chunk is a list)
        """
        if not lst:
            return []
            
        avg_chunk_size = max(1, len(lst) // num_chunks)
        chunks = []
        
        for i in range(0, len(lst), avg_chunk_size):
            chunks.append(lst[i:i + avg_chunk_size])
        
        return chunks
    
    def _export_image_paths(self, subset=None, prefix="", format="txt", free_memory=False):
        """Export image paths to a file in EXPORT_DIR.
        
        Args:
            subset: Specific subset of images to export, or None for all
            prefix: Prefix for the exported filename
            format: Export format ('txt', 'csv', or 'json')
            free_memory: Whether to delete the images from memory after export
        
        Returns:
            Path to the exported file
        """
        # Create export directory if it doesn't exist
        export_dir = Path(EXPORT_DIR)
        export_dir.mkdir(parents=True, exist_ok=True)
        
        # Use provided subset or all images
        img_paths_to_export = subset if subset is not None else self.imgs
        
        # Generate filename with timestamp for uniqueness
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dataset_name = self.__class__.__name__.lower().replace("dataset", "")
        
        # Convert Path objects to strings
        img_paths = [str(img_path) for img_path in img_paths_to_export]
        
        if format.lower() == "csv":
            filename = f"{prefix}_{dataset_name}_images_{timestamp}.csv" if prefix else f"{dataset_name}_images_{timestamp}.csv"
            export_path = export_dir / filename
            
            # Export as CSV with header
            df = pd.DataFrame({
                'image_path': img_paths,
                'frame_id': [Path(path).stem for path in img_paths]
            })
            df.to_csv(export_path, index=False)
            self.logger.success(f"Exported {len(img_paths)} image paths to CSV file {export_path}")
            
        elif format.lower() == "json":
            filename = f"{prefix}_{dataset_name}_images_{timestamp}.json" if prefix else f"{dataset_name}_images_{timestamp}.json"
            export_path = export_dir / filename
            
            # Export as JSON
            df = pd.DataFrame({
                'image_path': img_paths,
                'frame_id': [Path(path).stem for path in img_paths]
            })
            df.to_json(export_path, orient='records', lines=True)
            self.logger.success(f"Exported {len(img_paths)} image paths to JSON file {export_path}")
            
        else:
            filename = f"{prefix}_{dataset_name}_images_{timestamp}.txt" if prefix else f"{dataset_name}_images_{timestamp}.txt"
            export_path = export_dir / filename
            
            # Export as TXT
            with open(export_path, 'w') as f:
                f.write('\n'.join(img_paths))
            self.logger.success(f"Exported {len(img_paths)} image paths to TXT file {export_path}")
        
        # Free memory if requested
        if free_memory:
            if subset is None:  # If we exported everything
                self.logger.info("Freeing image data from memory")
                self.imgs = []
                import gc
                gc.collect()  # Force garbage collection
        
        return export_path
    
    def _export_metadata(self, subset=None, prefix="", format="parquet", free_memory=False):
        """Export metadata DataFrame to a file in EXPORT_DIR.
        
        Args:
            subset: Specific subset of metadata to export, or None for all
            prefix: Prefix for the exported filename
            format: Export format ('parquet' or 'csv')
            free_memory: Whether to delete the metadata from memory after export
            
        Returns:
            Path to the exported file
        """
        # Create export directory if it doesn't exist
        export_dir = Path(EXPORT_DIR)
        export_dir.mkdir(parents=True, exist_ok=True)
        
        # Use provided subset or all metadata
        md_to_export = subset if subset is not None else self.md
        
        # Generate filename with timestamp for uniqueness
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dataset_name = self.__class__.__name__.lower().replace("dataset", "")
        
        # Choose format - default to CSV if Parquet not available
        if format.lower() == "parquet" and PARQUET_AVAILABLE:
            filename = f"{prefix}_{dataset_name}_metadata_{timestamp}.parquet" if prefix else f"{dataset_name}_metadata_{timestamp}.parquet"
            export_path = export_dir / filename
            
            # Export DataFrame to Parquet
            table = pyarrow.Table.from_pandas(md_to_export)
            pq.write_table(table, export_path)
            self.logger.success(f"Exported {len(md_to_export)} metadata rows to Parquet file {export_path}")
        else:
            # Fall back to CSV
            if format.lower() == "parquet" and not PARQUET_AVAILABLE:
                self.logger.warning("Parquet format requested but pyarrow not available. Falling back to CSV.")
            
            filename = f"{prefix}_{dataset_name}_metadata_{timestamp}.csv" if prefix else f"{dataset_name}_metadata_{timestamp}.csv"
            export_path = export_dir / filename
            
            # Export DataFrame to CSV
            md_to_export.to_csv(export_path, index=False)
            self.logger.success(f"Exported {len(md_to_export)} metadata rows to CSV file {export_path}")
        
        # Free memory if requested
        if free_memory and subset is None:  # Only free memory if we exported everything
            self.logger.info("Freeing metadata from memory")
            self.md = pd.DataFrame()
            import gc
            gc.collect()  # Force garbage collection
        
        return export_path
    
    def _export_geospatial_metadata(self, subset=None, prefix="", format="geoparquet", 
                                    free_memory=False, crs="EPSG:4326"):
        """Export metadata with GPS coordinates as a GeoParquet file.
        
        Args:
            subset: Specific subset of metadata to export, or None for all
            prefix: Prefix for the exported filename
            format: Export format ('geoparquet' or 'geojson')
            free_memory: Whether to delete the metadata from memory after export
            crs: Coordinate reference system (default: EPSG:4326/WGS84)
            
        Returns:
            Path to the exported file
        """
        try:
            # Create export directory if it doesn't exist
            export_dir = Path(EXPORT_DIR)
            export_dir.mkdir(parents=True, exist_ok=True)
            
            # Use provided subset or all metadata
            md_to_export = subset if subset is not None else self.md
            
            # Check if the required columns are available
            if 'gps_info.latitude' not in md_to_export.columns or 'gps_info.longitude' not in md_to_export.columns:
                self.logger.error("Cannot export geospatial metadata: missing latitude/longitude columns")
                return None
                
            # Generate filename with timestamp for uniqueness
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            dataset_name = self.__class__.__name__.lower().replace("dataset", "")
            
            # Convert to GeoDataFrame with Point geometries
            self.logger.info("Converting metadata to GeoDataFrame with Point geometries")
            
            # Import geopandas here to avoid dependency issues
            try:
                import geopandas as gpd
                from shapely.geometry import Point
            except ImportError:
                self.logger.error("Cannot export GeoParquet: geopandas or shapely not installed")
                return None
            
            # Create Point geometries from lat/lon
            if PANDARALLEL_AVAILABLE and len(md_to_export) > 10000:
                self.logger.info("Using parallel processing to create geometries")
                md_to_export['geometry'] = md_to_export.parallel_apply(
                    lambda row: Point(row['gps_info.longitude'], row['gps_info.latitude']) 
                    if not pd.isna(row['gps_info.longitude']) and not pd.isna(row['gps_info.latitude']) 
                    else None, axis=1
                )
            else:
                md_to_export['geometry'] = md_to_export.apply(
                    lambda row: Point(row['gps_info.longitude'], row['gps_info.latitude']) 
                    if not pd.isna(row['gps_info.longitude']) and not pd.isna(row['gps_info.latitude']) 
                    else None, axis=1
                )
            
            # Drop rows with no geometry
            geospatial_df = md_to_export.dropna(subset=['geometry'])
            self.logger.info(f"Created {len(geospatial_df)} point geometries from GPS coordinates")
            
            # Convert to GeoDataFrame
            gdf = gpd.GeoDataFrame(geospatial_df, geometry='geometry', crs=crs)
            
            if format.lower() == "geojson":
                filename = f"{prefix}_{dataset_name}_geo_{timestamp}.geojson" if prefix else f"{dataset_name}_geo_{timestamp}.geojson"
                export_path = export_dir / filename
                gdf.to_file(export_path, driver="GeoJSON")
                self.logger.success(f"Exported {len(gdf)} geospatial records to GeoJSON file {export_path}")
            else:
                # Default to GeoParquet
                filename = f"{prefix}_{dataset_name}_geo_{timestamp}.parquet" if prefix else f"{dataset_name}_geo_{timestamp}.parquet"
                export_path = export_dir / filename
                
                # Save as GeoParquet with GeoArrow encoding (same approach as geo_processor_base.py)
                try:
                    gdf.to_parquet(
                        export_path,
                        compression='snappy',
                        geometry_encoding='geoarrow',
                        write_covering_bbox=True,
                        schema_version='1.1.0'
                    )
                    self.logger.success(f"Exported {len(gdf)} geospatial records to GeoParquet file {export_path}")
                except ImportError:
                    self.logger.error("Failed to save as GeoParquet: pyarrow package is required")
                    return None
                except Exception as e:
                    self.logger.warning(f"Failed to use GeoArrow encoding: {e}, falling back to WKB")
                    try:
                        gdf.to_parquet(
                            export_path,
                            compression='snappy',
                            geometry_encoding='WKB',
                            write_covering_bbox=True
                        )
                        self.logger.success(f"Exported {len(gdf)} geospatial records to GeoParquet file using WKB encoding (fallback)")
                    except Exception as e2:
                        self.logger.error(f"Failed to save with fallback method: {e2}")
                        return None
                        
            # Free memory if requested
            if free_memory and subset is None:
                self.logger.info("Freeing metadata from memory")
                self.md = pd.DataFrame()
                import gc
                gc.collect()
                
            return export_path
        except Exception as e:
            self.logger.error(f"Error exporting geospatial metadata: {e}")
            return None
    
    def align_images_with_metadata(self):
        """Align images with metadata to ensure perfect 1:1 matching."""
        self.logger.info("Aligning images with metadata...")
        
        if not self.imgs or self.md.empty:
            self.logger.warning("Cannot align: missing either images or metadata")
            return self.imgs, self.md
        
        # Use parallel processing for large datasets
        use_parallel = PANDARALLEL_AVAILABLE and len(self.imgs) > 10000
        
        # Create a DataFrame for images with frame_id
        if use_parallel:
            self.logger.info(f"Using parallel processing to extract frame_ids from {len(self.imgs)} images")
            img_df = pd.DataFrame({'image_path': [str(img) for img in self.imgs]})
            img_df['frame_id'] = img_df['image_path'].parallel_apply(lambda x: Path(x).stem)
        else:
            img_df = pd.DataFrame({
                'image_path': [str(img) for img in self.imgs],
                'frame_id': [Path(img).stem for img in self.imgs]
            })
        
        # Ensure metadata has frame_id column from image_ref
        if 'frame_id' not in self.md.columns:
            if use_parallel:
                self.md['frame_id'] = self.md['image_ref'].parallel_apply(lambda x: Path(x).stem)
            else:
                self.md['frame_id'] = self.md['image_ref'].apply(lambda x: Path(x).stem)
        
        # Drop the image_ref column as it's now redundant
        if 'image_ref' in self.md.columns:
            self.md = self.md.drop('image_ref', axis=1)
        
        # Check for duplicate frame_ids in metadata - this is likely the issue
        md_frame_counts = self.md['frame_id'].value_counts()
        duplicate_frames = md_frame_counts[md_frame_counts > 1].index.tolist()
        if duplicate_frames:
            duplicate_count = len(duplicate_frames)
            self.logger.warning(f"Found {duplicate_count} duplicate frame_ids in metadata! These will be removed.")
            sample_size = min(5, duplicate_count)
            self.logger.debug(f"Sample duplicate frame_ids: {duplicate_frames[:sample_size]}")
            
            # Keep only the first occurrence of each frame_id in metadata
            self.logger.info("Removing duplicate metadata entries...")
            self.md = self.md.drop_duplicates(subset=['frame_id'], keep='first')
        
        # Find the intersection of frame_ids (only keep images that have metadata and vice versa)
        common_frame_ids = set(img_df['frame_id']).intersection(set(self.md['frame_id']))
        
        # Log metadata rows without matching images
        metadata_only_frame_ids = set(self.md['frame_id']) - set(img_df['frame_id'])
        if metadata_only_frame_ids:
            self.logger.warning(f"Found {len(metadata_only_frame_ids)} metadata rows without matching images")
            sample_size = min(5, len(metadata_only_frame_ids))
            sample_ids = list(metadata_only_frame_ids)[:sample_size]
            self.logger.debug(f"Sample frame_ids without images: {sample_ids}")
        
        # Log images without matching metadata
        images_only_frame_ids = set(img_df['frame_id']) - set(self.md['frame_id'])
        if images_only_frame_ids:
            self.logger.warning(f"Found {len(images_only_frame_ids)} images without matching metadata")
            sample_size = min(5, len(images_only_frame_ids))
            sample_ids = list(images_only_frame_ids)[:sample_size]
            self.logger.debug(f"Sample frame_ids without metadata: {sample_ids}")
        
        self.logger.info(f"Found {len(common_frame_ids)} frame_ids common to both images and metadata")
        
        # Filter both datasets to only include common frame_ids
        img_df = img_df[img_df['frame_id'].isin(common_frame_ids)]
        filtered_md = self.md[self.md['frame_id'].isin(common_frame_ids)]
        
        # Log filtering results
        metadata_rows_removed = len(self.md) - len(filtered_md)
        self.logger.info(f"Removed {metadata_rows_removed} metadata rows without matching images")
        
        images_removed = len(self.imgs) - len(img_df)
        self.logger.info(f"Removed {images_removed} images without matching metadata")
        
        # Create indexes for faster lookup and ensure exact alignment
        self.logger.info("Creating aligned datasets with identical frame_id ordering...")
        
        # Create a mapping from frame_id to Path object
        img_map = {Path(path).stem: Path(path) for path in img_df['image_path']}
        
        # Set frame_id as index for metadata dataframe for faster lookup
        filtered_md = filtered_md.set_index('frame_id')
        
        # Create newly ordered arrays with perfect alignment
        sorted_frames = sorted(common_frame_ids)
        sorted_imgs = [img_map[frame_id] for frame_id in sorted_frames]
        sorted_md = filtered_md.loc[sorted_frames].reset_index()
        
        # Verify exact counts match
        if len(sorted_imgs) != len(sorted_md):
            self.logger.error(f"CRITICAL ERROR: Count mismatch after sorting: {len(sorted_imgs)} images vs {len(sorted_md)} metadata rows")
            raise ValueError("Failed to align images with metadata correctly")
        else:
            self.logger.success(f"Successfully aligned {len(sorted_imgs)} images with {len(sorted_md)} metadata rows")
        
        # Update the class attributes
        self.imgs = sorted_imgs
        self.md = sorted_md
        
        return sorted_imgs, sorted_md
        

class Nexar2020Dataset(BaseNexarDataset):
    """Dataset class for the Nexar 2020 dataset."""
    
    def load_img_dir(self, img_dir):
        """Load images from a directory.
        
        Args:
            img_dir: Path to the directory containing images
            
        Returns:
            List of image paths
        """
        return list(Path(img_dir).glob("*.jpg"))
    
    def _process_dir_chunk(self, dir_chunk):
        """Process a chunk of directories and return all image paths.
        
        Args:
            dir_chunk: List of directories to process
            
        Returns:
            List of image paths
        """
        all_images = []
        try:
            for img_dir in dir_chunk:
                try:
                    images = self.load_img_dir(img_dir)
                    all_images.extend(images)
                except Exception as e:
                    # Handle exceptions within individual directory processing
                    error_info = traceback.format_exc()
                    print(f"Error processing directory {img_dir}: {str(e)}\n{error_info}")
        except Exception as e:
            # Catch any other exceptions to prevent worker crashes
            error_info = traceback.format_exc()
            print(f"Unexpected error in worker process: {str(e)}\n{error_info}")
        
        return all_images
    
    def _process_md_chunk(self, file_chunk):
        """Process a chunk of metadata CSV files and return combined DataFrame.
        
        Args:
            file_chunk: List of CSV files to process
            
        Returns:
            DataFrame with combined metadata
        """
        dfs = []
        try:
            for file_path in file_chunk:
                try:
                    df = pd.read_csv(file_path)
                    dfs.append(df)
                except Exception as e:
                    # Log the error but continue with other files
                    error_info = traceback.format_exc()
                    print(f"Error reading {file_path}: {str(e)}\n{error_info}")
        except Exception as e:
            # Catch any other exceptions to prevent worker crashes
            error_info = traceback.format_exc()
            print(f"Unexpected error in worker process: {str(e)}\n{error_info}")
        
        if dfs:
            return pd.concat(dfs, ignore_index=True)
        else:
            return pd.DataFrame()

    @time_it(level="info", message="Loading images from Nexar 2020 dataset") 
    def load_images(self, export=False):
        """Load images from the October-November 2020 Nexar dataset.
        
        Args:
            export: If True, save the list of image paths to a file in EXPORT_DIR
        """
        static_img_dirs_path = Path(NEXAR_2020) / "imgs" / "oct_15-nov-15"
        static_img_dirs = list(static_img_dirs_path.glob("*"))
        
        # Distribute directories evenly across CPUs
        chunked_dirs = self._chunk_list(static_img_dirs, self.ncpus)
        
        futures = []
        with ProcessPoolExecutor(max_workers=self.ncpus) as executor:
            # Submit one task per CPU, where each task processes a chunk of directories
            for chunk in chunked_dirs:
                futures.append(executor.submit(self._process_dir_chunk, chunk))
            
            # Wait for all futures to complete and collect results
            for future in futures:
                result = future.result()  # This will be a list of image paths
                self.logger.info(f"Loaded {len(result)} images from task")
                self.imgs.extend(result)
        
        self.logger.success(f"Loaded {len(self.imgs)} images from Nexar 2020 dataset")
        
        # Align with metadata if it's already loaded
        if not self.md.empty:
            self.logger.info("Metadata already loaded, aligning with images...")
            self.align_images_with_metadata()
        
        # Export image paths to file if requested
        if export:
            self._export_image_paths()
        
        return self.imgs
                
    @time_it(level="info", message="Loading metadata from Nexar 2020 dataset")
    def load_metadata(self, export=False, format="parquet"): 
        """Load metadata from the dataset.
        
        Args:
            export: If True, save the metadata DataFrame to a file in EXPORT_DIR
            format: Export format ('parquet' or 'csv')
        """
        try:
            # Try with parallel processing first
            return self._load_metadata_parallel(export, format)
        except Exception as e:
            self.logger.warning(f"Parallel processing failed: {str(e)}. Falling back to sequential processing.")
            return self._load_metadata_sequential(export, format)

    def _load_metadata_parallel(self, export=False, format="parquet"):
        """Load metadata using parallel processing."""
        static_md_dirs_path = Path(NEXAR_2020) / "metadata" / "oct_15-nov-15"
        static_md_files = list(static_md_dirs_path.glob("16*.csv"))

        chunked_files = self._chunk_list(static_md_files, self.ncpus)

        futures = []
        with ProcessPoolExecutor(max_workers=self.ncpus) as executor:
            for chunk in chunked_files:
                futures.append(executor.submit(self._process_md_chunk, chunk))

            dfs = []
            for future in futures:
                result = future.result()
                self.logger.info(f"Loaded {len(result)} rows from metadata task")
                dfs.append(result)
            
            # Process results as before
            self._process_metadata_results(dfs, export, format)
            
        return self.md

    def _load_metadata_sequential(self, export=False, format="parquet"):
        """Load metadata sequentially as a fallback."""
        static_md_dirs_path = Path(NEXAR_2020) / "metadata" / "oct_15-nov-15"
        static_md_files = list(static_md_dirs_path.glob("16*.csv"))
        
        dfs = []
        total_files = len(static_md_files)
        
        for i, file_path in enumerate(static_md_files):
            if i % 10 == 0:  # Log progress every 10 files
                self.logger.info(f"Processing file {i+1}/{total_files}")
                
            try:
                df = pd.read_csv(file_path)
                dfs.append(df)
            except Exception as e:
                self.logger.error(f"Error reading {file_path}: {str(e)}")
                
        # Process results
        self._process_metadata_results(dfs, export, format)
        
        return self.md

    def _process_metadata_results(self, dfs, export=False, format="parquet"):
        """Common processing for metadata results."""
        if dfs:
            log_memory_usage(self.logger, "Before metadata concat")
            self.md = pd.concat(dfs, ignore_index=True)
            log_memory_usage(self.logger, "After metadata concat")
            
            # Use parallel processing for large datasets
            if PANDARALLEL_AVAILABLE and len(self.md) > 10000:
                self.logger.info("Using parallel processing for metadata transformations")
                self.md['frame_id'] = self.md['image_ref'].parallel_apply(lambda x: Path(x).stem)
                self.md['timestamp'] = pd.to_datetime(self.md['timestamp'], unit='ms')
                self.md['timestamp'] = self.md['timestamp'].parallel_apply(
                    lambda x: x.tz_localize('UTC').tz_convert('US/Eastern')
                )
            else:
                # Standard processing
                self.md['frame_id'] = self.md['image_ref'].apply(lambda x: Path(x).stem)
                self.md['timestamp'] = pd.to_datetime(self.md['timestamp'], unit='ms')
                self.md['timestamp'] = self.md['timestamp'].dt.tz_localize('UTC').dt.tz_convert('US/Eastern')
            
            log_memory_usage(self.logger, "After timestamp conversion")
            self.logger.success(f"Loaded {len(self.md)} metadata rows from Nexar 2020 dataset")
            
            # Align images with metadata if both are loaded
            if self.imgs:
                self.logger.info("Images already loaded, aligning with metadata...")
                self.align_images_with_metadata()
            
            # Export metadata to file if requested
            if export:
                self._export_metadata(format=format)
        else:
            self.logger.warning("No metadata files were loaded")
            self.md = pd.DataFrame()


class Nexar2023Dataset(BaseNexarDataset): 
    """Dataset class for the Nexar 2023 dataset."""
    
    def load_img_dir(self, img_dir):
        """Load images from a directory.
        
        Args:
            img_dir: Path to the directory containing images
            
        Returns:
            List of image paths
        """
        return list(Path(img_dir).glob("*.jpg"))
    
    def _process_dir_chunk(self, dir_chunk):
        """Process a chunk of directories and return all image paths.
        
        Args:
            dir_chunk: List of directories to process
            
        Returns:
            List of image paths
        """
        all_images = []
        for img_dir in dir_chunk:
            images = self.load_img_dir(img_dir)
            all_images.extend(images)
        return all_images
    
    def _process_md_chunk(self, file_chunk):
        """Process a chunk of metadata CSV files and return combined DataFrame.
        
        Args:
            file_chunk: List of CSV files to process
            
        Returns:
            DataFrame with combined metadata
        """
        dfs = []
        for file_path in file_chunk:
            try:
                df = pd.read_csv(file_path)
                dfs.append(df)
            except Exception as e:
                self.logger.error(f"Error reading {file_path}: {str(e)}")
        
        if dfs:
            return pd.concat(dfs, ignore_index=True)
        else:
            return pd.DataFrame()
    
    @time_it(level="info", message="Loading images from Nexar 2023 dataset")
    def load_images(self, export=False):
        """Load images from the Nexar 2023 dataset.
        
        Args:
            export: If True, save the list of image paths to a file in EXPORT_DIR
        """
        # Placeholder implementation - follow the same pattern as 2020 dataset
        self.logger.warning("Nexar 2023 image loading not fully implemented yet")
        
        if export:
            self._export_image_paths()
            
        return self.imgs
    
    @time_it(level="info", message="Loading metadata from Nexar 2023 dataset")
    def load_metadata(self, export=False, format="parquet"):
        """Load metadata from the Nexar 2023 dataset.
        
        Args:
            export: If True, save the metadata DataFrame to a file in EXPORT_DIR
            format: Export format ('parquet' or 'csv')
        """
        # Placeholder implementation - follow the same pattern as 2020 dataset
        self.logger.warning("Nexar 2023 metadata loading not fully implemented yet")
        
        if export:
            self._export_metadata(format=format)
            
        return self.md


# ===== Helper Functions =====

def filter_dataset_by_date(dataset, start_date=None, end_date=None, export=False, format="parquet"):
    """Filter a dataset by date range."""
    logger = get_logger("filter_dataset")
    
    # Log initial memory state
    log_memory_usage(logger, "Before filtering")
    
    # Check if metadata is available
    if dataset.md.empty:
        logger.warning("No metadata available for filtering. Load metadata first.")
        return dataset.imgs, dataset.md
    
    # If no dates specified, return the original dataset
    if not start_date and not end_date:
        logger.info("No date range specified, returning original dataset")
        return dataset.imgs, dataset.md
    
    # Convert string dates to datetime
    start = pd.to_datetime(start_date) if start_date else pd.Timestamp.min
    end = pd.to_datetime(end_date) if end_date else pd.Timestamp.max
    
    logger.info(f"Filtering dataset by date range: {start.date()} to {end.date()}")
    
    # Ensure timestamp column is properly converted to datetime
    if 'timestamp' in dataset.md.columns:
        # Check if timestamp is already in datetime format
        if not pd.api.types.is_datetime64_any_dtype(dataset.md['timestamp']):
            logger.info("Converting timestamp column to datetime format")
            
            # Use parallel processing if available
            if PANDARALLEL_AVAILABLE:
                logger.info("Using parallel processing for timestamp conversion")
                # Convert from epoch milliseconds to datetime with timezone
                dataset.md['timestamp'] = pd.to_datetime(dataset.md['timestamp'], unit='ms')
                dataset.md['timestamp'] = dataset.md['timestamp'].parallel_apply(
                    lambda x: x.tz_localize('UTC').tz_convert('US/Eastern')
                )
            else:
                # Standard processing
                dataset.md['timestamp'] = pd.to_datetime(dataset.md['timestamp'], unit='ms')
                dataset.md['timestamp'] = dataset.md['timestamp'].dt.tz_localize('UTC').dt.tz_convert('US/Eastern')
                
            logger.info(f"Timestamp sample: {dataset.md['timestamp'].iloc[0]} (converted from epoch time)")
    else:
        logger.error("No timestamp column found in metadata. Cannot filter by date.")
        return dataset.imgs, dataset.md
    
    # Filter metadata by date range (this is a vectorized operation so doesn't need parallelization)
    filtered_md = dataset.md[(dataset.md['timestamp'].dt.date >= start.date()) & 
                           (dataset.md['timestamp'].dt.date <= end.date())]
    
    log_memory_usage(logger, "After metadata filtering")
    
    # Filter images by frame_id using parallel operations if available
    filtered_imgs = []
    if dataset.imgs:
        filtered_frame_ids = set(filtered_md['frame_id'])
        
        # Use parallel processing for large image lists
        if len(dataset.imgs) > 10000 and PANDARALLEL_AVAILABLE:
            logger.info(f"Using parallel processing to filter {len(dataset.imgs)} images")
            # Create a DataFrame for parallel filtering
            img_df = pd.DataFrame({'path': dataset.imgs})
            img_df['frame_id'] = img_df['path'].parallel_apply(lambda x: Path(x).stem)
            img_df['keep'] = img_df['frame_id'].parallel_apply(lambda x: x in filtered_frame_ids)
            filtered_imgs = [Path(p) for p in img_df[img_df['keep']]['path']]
        else:
            # Standard filtering for smaller lists
            filtered_imgs = [img for img in dataset.imgs if Path(img).stem in filtered_frame_ids]
    
    log_memory_usage(logger, "After image filtering")
    
    logger.success(f"Filtered dataset contains {len(filtered_imgs)} images and {len(filtered_md)} metadata rows")
    
    # Export if requested
    if export:
        date_range = f"{start.strftime('%Y%m%d')}-{end.strftime('%Y%m%d')}"
        
        if filtered_imgs:
            dataset._export_image_paths(subset=filtered_imgs, prefix=f"filtered_{date_range}")
        
        if not filtered_md.empty:
            dataset._export_metadata(subset=filtered_md, prefix=f"filtered_{date_range}", format=format)
    
    return filtered_imgs, filtered_md

def filter_dataset_by_specific_dates(dataset, dates, export=False, format="parquet"):
    """Filter a dataset to include only records from specific dates.
    
    Args:
        dataset: The dataset to filter
        dates: List of dates as strings (YYYY-MM-DD) or datetime objects
        export: Whether to export the filtered data
        format: Export format for metadata ('parquet' or 'csv')
        
    Returns:
        Tuple of (filtered_images, filtered_metadata)
    """
    logger = get_logger("filter_dataset_dates")
    
    # Log initial memory state
    log_memory_usage(logger, "Before filtering")
    
    # Check if metadata is available
    if dataset.md.empty:
        logger.warning("No metadata available for filtering. Load metadata first.")
        return dataset.imgs, dataset.md
    
    # If no dates specified, return the original dataset
    if not dates or len(dates) == 0:
        logger.info("No dates specified, returning original dataset")
        return dataset.imgs, dataset.md
    
    # Convert string dates to datetime objects
    parsed_dates = []
    for date_str in dates:
        try:
            # Handle both date objects and strings
            if isinstance(date_str, (datetime, pd.Timestamp)):
                parsed_dates.append(pd.Timestamp(date_str).date())
            else:
                # Try different formats
                try:
                    # Try standard ISO format first
                    parsed_dates.append(pd.to_datetime(date_str).date())
                except:
                    # Try other common formats if ISO fails
                    formats = ['%Y-%m-%d', '%m-%d-%Y', '%m/%d/%Y', '%d-%m-%Y']
                    for fmt in formats:
                        try:
                            parsed_dates.append(pd.to_datetime(date_str, format=fmt).date())
                            break
                        except:
                            continue
                    else:
                        logger.warning(f"Couldn't parse date: {date_str}, skipping")
        except Exception as e:
            logger.warning(f"Error parsing date {date_str}: {e}")
    
    if not parsed_dates:
        logger.warning("No valid dates found in the provided list")
        return dataset.imgs, dataset.md
    
    logger.info(f"Filtering dataset for {len(parsed_dates)} specific dates: {', '.join(str(d) for d in parsed_dates)}")
    
    # Ensure timestamp column is properly converted to datetime
    if 'timestamp' in dataset.md.columns:
        # Check if timestamp is already in datetime format
        if not pd.api.types.is_datetime64_any_dtype(dataset.md['timestamp']):
            logger.info("Converting timestamp column to datetime format")
            
            # Use parallel processing if available
            if PANDARALLEL_AVAILABLE:
                logger.info("Using parallel processing for timestamp conversion")
                # Convert from epoch milliseconds to datetime with timezone
                dataset.md['timestamp'] = pd.to_datetime(dataset.md['timestamp'], unit='ms')
                dataset.md['timestamp'] = dataset.md['timestamp'].parallel_apply(
                    lambda x: x.tz_localize('UTC').tz_convert('US/Eastern')
                )
            else:
                # Standard processing
                dataset.md['timestamp'] = pd.to_datetime(dataset.md['timestamp'], unit='ms')
                dataset.md['timestamp'] = dataset.md['timestamp'].dt.tz_localize('UTC').dt.tz_convert('US/Eastern')
                
            logger.info(f"Timestamp sample: {dataset.md['timestamp'].iloc[0]} (converted from epoch time)")
    else:
        logger.error("No timestamp column found in metadata. Cannot filter by date.")
        return dataset.imgs, dataset.md
    
    # Filter metadata to include only records from the specified dates
    # This converts timestamp to date and checks if it's in our list of parsed dates
    filtered_md = dataset.md[dataset.md['timestamp'].dt.date.isin(parsed_dates)]
    
    log_memory_usage(logger, "After metadata filtering")
    
    # Filter images by frame_id using parallel operations if available
    filtered_imgs = []
    if dataset.imgs:
        filtered_frame_ids = set(filtered_md['frame_id'])
        
        # Use parallel processing for large image lists
        if len(dataset.imgs) > 10000 and PANDARALLEL_AVAILABLE:
            logger.info(f"Using parallel processing to filter {len(dataset.imgs)} images")
            # Create a DataFrame for parallel filtering
            img_df = pd.DataFrame({'path': dataset.imgs})
            img_df['frame_id'] = img_df['path'].parallel_apply(lambda x: Path(x).stem)
            img_df['keep'] = img_df['frame_id'].parallel_apply(lambda x: x in filtered_frame_ids)
            filtered_imgs = [Path(p) for p in img_df[img_df['keep']]['path']]
        else:
            # Standard filtering for smaller lists
            filtered_imgs = [img for img in dataset.imgs if Path(img).stem in filtered_frame_ids]
    
    log_memory_usage(logger, "After image filtering")
    
    logger.success(f"Filtered dataset contains {len(filtered_imgs)} images and {len(filtered_md)} metadata rows")
    
    # Export if requested
    if export:
        date_str = "specific_dates"
        if len(parsed_dates) <= 3:
            # Use abbreviated format for small number of dates
            date_str = "_".join([d.strftime('%Y%m%d') for d in parsed_dates])
        
        if filtered_imgs:
            dataset._export_image_paths(subset=filtered_imgs, prefix=f"filtered_{date_str}")
        
        if not filtered_md.empty:
            dataset._export_metadata(subset=filtered_md, prefix=f"filtered_{date_str}", format=format)
    
    return filtered_imgs, filtered_md

def export_dataset_statistics(dataset, output_file=None):
    """Generate and export statistics about a dataset.
    
    Args:
        dataset: The dataset to analyze
        output_file: Path to output file, or None to print to console
        
    Returns:
        Dictionary containing statistics
    """
    logger = get_logger("dataset_stats")
    
    stats = {
        "dataset_type": dataset.__class__.__name__,
        "images": {},
        "metadata": {}
    }
    
    # Image statistics
    if dataset.imgs:
        stats["images"]["count"] = len(dataset.imgs)
        
        # Directory statistics
        dirs = {}
        for img in dataset.imgs:
            parent = str(Path(img).parent)
            if parent in dirs:
                dirs[parent] += 1
            else:
                dirs[parent] = 1
        
        stats["images"]["directories"] = {
            "count": len(dirs),
            "avg_images_per_dir": sum(dirs.values()) / len(dirs) if dirs else 0
        }
    else:
        stats["images"]["count"] = 0
    
    # Metadata statistics
    if not dataset.md.empty:
        stats["metadata"]["count"] = len(dataset.md)
        
        # Date range
        min_date = dataset.md['timestamp'].min()
        max_date = dataset.md['timestamp'].max()
        stats["metadata"]["date_range"] = {
            "start": min_date.strftime("%Y-%m-%d"),
            "end": max_date.strftime("%Y-%m-%d")
        }
        
        # Statistics by day
        day_counts = dataset.md.groupby(dataset.md['timestamp'].dt.date).size()
        stats["metadata"]["by_day"] = {
            "num_days": len(day_counts),
            "avg_rows_per_day": float(day_counts.mean()),
            "min_rows": {
                "count": int(day_counts.min()),
                "date": day_counts.idxmin().strftime("%Y-%m-%d")
            },
            "max_rows": {
                "count": int(day_counts.max()),
                "date": day_counts.idxmax().strftime("%Y-%m-%d")
            }
        }
    else:
        stats["metadata"]["count"] = 0
    
    # Format output
    output_text = []
    output_text.append(f"\n===== {stats['dataset_type']} Statistics =====")
    
    if stats["images"]["count"] > 0:
        output_text.append("\nImages:")
        output_text.append(f"  Total count: {stats['images']['count']}")
        output_text.append(f"  Number of directories: {stats['images']['directories']['count']}")
        output_text.append(f"  Average images per directory: {stats['images']['directories']['avg_images_per_dir']:.1f}")
    else:
        output_text.append("\nImages: None loaded")
    
    if stats["metadata"]["count"] > 0:
        output_text.append("\nMetadata:")
        output_text.append(f"  Total rows: {stats['metadata']['count']}")
        output_text.append(f"  Date range: {stats['metadata']['date_range']['start']} to {stats['metadata']['date_range']['end']}")
        output_text.append(f"  Number of days: {stats['metadata']['by_day']['num_days']}")
        output_text.append(f"  Average rows per day: {stats['metadata']['by_day']['avg_rows_per_day']:.1f}")
        output_text.append(f"  Min rows on a day: {stats['metadata']['by_day']['min_rows']['count']} ({stats['metadata']['by_day']['min_rows']['date']})")
        output_text.append(f"  Max rows on a day: {stats['metadata']['by_day']['max_rows']['count']} ({stats['metadata']['by_day']['max_rows']['date']})")
    else:
        output_text.append("\nMetadata: None loaded")
    
    output_text.append("\n=========================================")
    
    # Format as string
    output_str = "\n".join(output_text)
    
    # Output to file or console
    if output_file:
        with open(output_file, 'w') as f:
            f.write(output_str)
        logger.success(f"Statistics exported to {output_file}")
    else:
        print(output_str)
    
    return stats


def split_dataset_into_chunks(dataset, num_chunks=2, export=True, format="parquet", prefix="chunk", free_memory=True):
    """Split a dataset into evenly-sized chunks for distributed processing.
    
    This function is useful for dividing a dataset across multiple GPUs or nodes.
    The function preserves alignment between images and metadata in each chunk.
    
    Args:
        dataset: The dataset to split
        num_chunks: Number of chunks to create
        export: Whether to export the chunks to files
        format: Export format for metadata ('parquet' or 'csv')
        prefix: Prefix for exported files
        free_memory: Whether to delete chunks from memory after export
    
    Returns:
        List of (chunk_images, chunk_metadata) tuples, or just export paths if free_memory=True
    """
    logger = get_logger("split_dataset")
    
    # First ensure that images and metadata are aligned
    validate_image_metadata_alignment(dataset)
    
    # Calculate chunk sizes
    total_items = len(dataset.imgs)  # Should be the same as len(dataset.md) after validation
    
    if total_items == 0:
        logger.warning("Dataset is empty, cannot split")
        return []
    
    # Adjust num_chunks if we have fewer items than requested chunks
    if total_items < num_chunks:
        logger.warning(f"Fewer items ({total_items}) than requested chunks ({num_chunks}). Adjusting to {total_items} chunks.")
        num_chunks = total_items
    
    # Calculate base chunk size and remainder
    base_chunk_size = total_items // num_chunks
    remainder = total_items % num_chunks
    
    logger.info(f"Splitting dataset with {total_items} items into {num_chunks} chunks")
    
    # Create chunks
    chunks = []
    export_paths = []
    start_idx = 0
    
    for i in range(num_chunks):
        # Add one extra item to early chunks if we have a remainder
        chunk_size = base_chunk_size + (1 if i < remainder else 0)
        end_idx = start_idx + chunk_size
        
        # Extract chunk data
        chunk_imgs = dataset.imgs[start_idx:end_idx]
        chunk_md = dataset.md.iloc[start_idx:end_idx].copy()
        
        logger.info(f"Chunk {i+1}: {len(chunk_imgs)} items (indices {start_idx}-{end_idx-1})")
        
        # Export if requested
        if export:
            chunk_prefix = f"{prefix}_{i+1}_of_{num_chunks}"
            
            # Create a temporary dataset for export
            temp_dataset = type(dataset)(load_imgs=False, load_md=False, ncpus=dataset.ncpus)
            temp_dataset.imgs = chunk_imgs
            temp_dataset.md = chunk_md
            
            # Export
            img_path = temp_dataset._export_image_paths(prefix=chunk_prefix)
            md_path = temp_dataset._export_metadata(format=format, prefix=chunk_prefix)
            
            export_paths.append((img_path, md_path))
            logger.success(f"Exported chunk {i+1} to {img_path} and {md_path}")
            
            # Free memory after export if requested
            if free_memory:
                # Delete temporary dataset to free memory
                del temp_dataset
                # Don't store chunks in memory
                continue
        
        # Add to results (only if we're not freeing memory)
        chunks.append((chunk_imgs, chunk_md))
        
        # Update start index for next chunk
        start_idx = end_idx
    
    logger.success(f"Successfully split dataset into {num_chunks} chunks")
    
    # Return export paths if we freed memory, otherwise return chunks
    if export and free_memory:
        import gc
        gc.collect()  # Force garbage collection
        return export_paths
    else:
        return chunks


def validate_image_metadata_alignment(dataset, fix_misalignment=True):
    """Validate and optionally fix the alignment between images and metadata.
    
    Args:
        dataset: The dataset to validate
        fix_misalignment: Whether to fix misalignments by dropping unmatched entries
        
    Returns:
        Boolean indicating whether the dataset is aligned (after fixing if requested)
    """
    logger = get_logger("validate_alignment")
    
    if not dataset.imgs or dataset.md.empty:
        logger.warning("Cannot validate: missing either images or metadata")
        return False
    
    # Extract frame_ids from images and metadata
    img_frame_ids = [Path(img).stem for img in dataset.imgs]
    md_frame_ids = dataset.md['frame_id'].tolist()
    
    # Check if lengths match
    if len(img_frame_ids) != len(md_frame_ids):
        logger.warning(f"Mismatched length: {len(img_frame_ids)} images vs {len(md_frame_ids)} metadata rows")
        is_aligned = False
    else:
        # Check if corresponding frame_ids match
        mismatches = sum(1 for i, (img_id, md_id) in enumerate(zip(img_frame_ids, md_frame_ids)) if img_id != md_id)
        if mismatches > 0:
            logger.warning(f"Found {mismatches} frame_id mismatches between images and metadata")
            is_aligned = False
        else:
            logger.success(f"Perfect alignment: {len(img_frame_ids)} images match exactly with metadata rows")
            is_aligned = True
    
    # Fix misalignment if requested and needed
    if not is_aligned and fix_misalignment:
        logger.info("Fixing misalignment by ensuring perfect 1:1 mapping between images and metadata")
        # Force re-alignment of images and metadata
        dataset.align_images_with_metadata()
        
        # Verify the fix worked
        if len(dataset.imgs) == len(dataset.md):
            fixed_img_ids = [Path(img).stem for img in dataset.imgs]
            fixed_md_ids = dataset.md['frame_id'].tolist()
            mismatches = sum(1 for i, (img_id, md_id) in enumerate(zip(fixed_img_ids, fixed_md_ids)) if img_id != md_id)
            
            if mismatches == 0:
                logger.success(f"Successfully fixed alignment: {len(dataset.imgs)} images match exactly with metadata rows")
                is_aligned = True
            else:
                logger.error(f"Failed to fix alignment: still have {mismatches} mismatches")
        else:
            logger.error(f"Failed to fix alignment: still have length mismatch ({len(dataset.imgs)} images vs {len(dataset.md)} metadata rows)")
    
    return is_aligned


def get_memory_usage(detailed=False):
    """Get current memory usage information.
    
    Args:
        detailed: If True, return detailed memory statistics
        
    Returns:
        Dictionary with memory usage information
    """
    if not PSUTIL_AVAILABLE:
        return {"available": False, "message": "psutil not installed"}
    
    process = psutil.Process()
    memory_info = process.memory_info()
    
    # Basic memory usage
    memory_usage = {
        "available": True,
        "rss_mb": memory_info.rss / (1024 * 1024),  # Resident Set Size in MB
        "vms_mb": memory_info.vms / (1024 * 1024),  # Virtual Memory Size in MB
    }
    
    # Add system-wide memory info
    system_memory = psutil.virtual_memory()
    memory_usage["system"] = {
        "total_gb": system_memory.total / (1024**3),
        "available_gb": system_memory.available / (1024**3),
        "percent_used": system_memory.percent
    }
    
    if detailed:
        # Add more detailed memory info
        memory_usage["detailed"] = {
            "uss_mb": getattr(memory_info, 'uss', 0) / (1024 * 1024),  # Unique Set Size if available
            "swap_mb": getattr(memory_info, 'swap', 0) / (1024 * 1024),  # Swap memory if available
        }
    
    return memory_usage

def log_memory_usage(logger, operation_name=""):
    """Log the current memory usage.
    
    Args:
        logger: Logger to use
        operation_name: Name of the operation being performed
    """
    if not PSUTIL_AVAILABLE:
        return
    
    memory = get_memory_usage()
    prefix = f"[{operation_name}] " if operation_name else ""
    
    logger.info(f"{prefix}Memory usage: {memory['rss_mb']:.1f} MB (RSS), "
                f"System: {memory['system']['percent_used']}% used, "
                f"{memory['system']['available_gb']:.1f} GB available")


# Now let's modify the load_nexar_dataset function to use these new functions
def load_nexar_dataset(dataset_year="2020", imgs=True, md=True, ncpus=8, 
                      start_date=None, end_date=None, dates=None, export=True, 
                      format="parquet", export_geoparquet=True, no_stats=False, 
                      validate_align=True, split_chunks=None, 
                      free_memory_after_export=True, memory_tracking=True):
    """Load a Nexar dataset, optionally filter by date, and show statistics.
    
    Args:
        dataset_year: Year of the dataset ('2020' or '2023')
        imgs: Whether to load images
        md: Whether to load metadata
        ncpus: Number of CPUs to use for parallel processing
        start_date: Start date for range filtering (YYYY-MM-DD)
        end_date: End date for range filtering (YYYY-MM-DD)
        dates: List of specific dates to filter for (overrides start_date/end_date if provided)
        export: Whether to export the dataset
        format: Export format for metadata ('parquet' or 'csv')
        export_geoparquet: Whether to export geospatial metadata as GeoParquet
        no_stats: If True, don't show dataset statistics
        validate_align: Whether to validate image-metadata alignment
        split_chunks: Number of chunks to split the dataset into (None for no splitting)
        free_memory_after_export: Whether to free memory after exporting
        memory_tracking: Whether to track memory usage
    
    Returns:
        Loaded dataset or result of splitting
    """
    # If ncpus is not specified, use the number of available cores
    if ncpus is None:
        import multiprocessing
        ncpus = multiprocessing.cpu_count()
    
    logger = get_logger("load_dataset")
    logger.info(f"Loading Nexar {dataset_year} dataset with {ncpus} CPUs...")
    
    if memory_tracking and PSUTIL_AVAILABLE:
        log_memory_usage(logger, "Initial state")
    
    # Choose dataset class based on year
    if (dataset_year == "2020") or (dataset_year == 2020):
        dataset_class = Nexar2020Dataset
    elif (dataset_year == "2023") or (dataset_year == 2023):
        dataset_class = Nexar2023Dataset
    else:
        raise ValueError(f"Invalid dataset year: {dataset_year}. Must be '2020' or '2023'")
    
    # Create dataset with no automatic loading
    dataset = dataset_class(load_imgs=False, load_md=False, ncpus=ncpus)
    
    # Load images if requested
    if imgs:
        dataset.load_images(export=False)  # Don't export yet
        if memory_tracking and PSUTIL_AVAILABLE:
            log_memory_usage(logger, "After loading images")
    
    # Load metadata if requested
    if md:
        dataset.load_metadata(export=False, format=format)  # Don't export yet
        if memory_tracking and PSUTIL_AVAILABLE:
            log_memory_usage(logger, "After loading metadata")
    
    # Validate alignment if requested and both images and metadata are loaded
    if validate_align and imgs and md and dataset.imgs and not dataset.md.empty:
        logger.info("Validating image-metadata alignment...")
        validate_image_metadata_alignment(dataset, fix_misalignment=True)
    
    # Apply date filtering if specified
    if md and not dataset.md.empty:
        if dates:  # Specific dates filtering takes precedence
            logger.info(f"Filtering dataset by {len(dates)} specific dates")
            filtered_imgs, filtered_md = filter_dataset_by_specific_dates(
                dataset, dates, export=False, format=format
            )
            # Update dataset with filtered data
            dataset.imgs = filtered_imgs
            dataset.md = filtered_md
            logger.success(f"Dataset filtered to {len(filtered_imgs)} images and {len(filtered_md)} metadata rows")
        elif start_date or end_date:  # Range-based filtering
            logger.info(f"Filtering dataset by date range: {start_date} to {end_date}")
            filtered_imgs, filtered_md = filter_dataset_by_date(
                dataset, start_date, end_date, export=False, format=format
            )
            # Update dataset with filtered data
            dataset.imgs = filtered_imgs
            dataset.md = filtered_md
            logger.success(f"Dataset filtered to {len(filtered_imgs)} images and {len(filtered_md)} metadata rows")
        
        if memory_tracking and PSUTIL_AVAILABLE:
            log_memory_usage(logger, "After date filtering")
    
    # Split dataset into chunks if requested
    if split_chunks is not None and split_chunks > 1:
        # Show statistics before splitting (and potentially freeing memory)
        if not no_stats:
            logger.info("Showing original dataset statistics (before splitting)...")
            export_dataset_statistics(dataset)
            
        logger.info(f"Splitting dataset into {split_chunks} chunks...")
        results = split_dataset_into_chunks(
            dataset, 
            num_chunks=split_chunks, 
            export=export, 
            format=format,
            free_memory=free_memory_after_export
        )
        
        if memory_tracking and PSUTIL_AVAILABLE and not free_memory_after_export:
            log_memory_usage(logger, "After dataset splitting")
        
        # Free memory after chunking if requested
        if free_memory_after_export:
            logger.info("Freeing original dataset from memory")
            del dataset.imgs
            del dataset.md
            import gc
            gc.collect()  # Force garbage collection
        
        return results
    
    # Show statistics BEFORE exporting and freeing memory
    if not no_stats:
        logger.info("Showing dataset statistics...")
        export_dataset_statistics(dataset)
    
    # Export after showing statistics
    if export:
        if imgs and dataset.imgs:
            dataset._export_image_paths(free_memory=free_memory_after_export)
        if md and not dataset.md.empty:
            # Standard metadata export
            dataset._export_metadata(format=format, free_memory=False)  # Don't free memory yet
            
            # Also export as GeoParquet if requested
            if export_geoparquet:
                dataset._export_geospatial_metadata(format="geoparquet", free_memory=free_memory_after_export)
            elif free_memory_after_export:
                # If we didn't export geoparquet but still want to free memory
                import gc
                dataset.md = pd.DataFrame()
                gc.collect()
        
        if memory_tracking and PSUTIL_AVAILABLE and not free_memory_after_export:
            log_memory_usage(logger, "After exporting data")
    
    logger.success(f"Dataset loaded: {len(dataset.imgs)} images and {len(dataset.md)} metadata rows")
    
    return dataset


if __name__ == "__main__":
    # Expose simplified CLI with just the load function
    fire.Fire(load_nexar_dataset)




