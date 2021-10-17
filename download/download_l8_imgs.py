import json
import time
import argparse
import re
import threading
from datetime import datetime
from pathlib import Path
# make packages in different folders visible
import sys
sys.path.insert(1, "../lambda/")

import boto3

import util
from util import get_credentials, send_request, threaded_download, threads, s3_join, upload_to_s3
from process_l8_imgs import calc_ndvi_and_mask_l8_clouds


# --------------------------------------------------------------------------- #
# Code adapted from UGSS M2M API sample script
# https://m2m.cr.usgs.gov/api/docs/example/download_landsat_c2-py
# --------------------------------------------------------------------------- #


""" Log in to the API and return the API key to use with future requests. """
def authenticate(url, username=None, password=None, verbose=True):
    # To access the API, first register for USGS Eros credentials here: https://ers.cr.usgs.gov/register
    # Next, log in and request access to the M2M API here: https://ers.cr.usgs.gov/profile/access
    # It might take a few days to be granted access.
    if not username or not password:
        username, password = get_credentials("USGS Eros Username: ")
    payload = {'username': username, 'password': password}
    if verbose: print("Logging in...")
    api_key = send_request(url + "login", payload, verbose=verbose)
    return api_key


""" Search for images matching a certain criteria, and return a list of products that
    can be downloaded. date_range and cloud_range should be tuples, and boundary should
    point to a GeoJSON file. Reserving **kwargs to be used in the future if needed. """
def search(url, api_key, dataset=None, max_results=50, date_range=None, cloud_range=None, boundary=None, verbose=True, **kwargs):
    if verbose: print("Fetching scenes...")
    
    # search for scenes that match our criteria
    acquisition_filter = cloud_cover_filter = ingest_filter = metadata_filter = seasonal_filter = spatial_filter = None
    
    if date_range:
        acquisition_filter = {
            'start': date_range[0],
            'end': date_range[1]
        }
        
    if cloud_range:
        cloud_cover_filter = {
            'min': cloud_range[0],
            'max': cloud_range[1],
            'includeUnknown': True
        }
    
    if boundary:
        with open(boundary) as file:
            geojson = format_geojson(file)
        
        spatial_filter = {
            'filterType': 'geojson',
            'geoJson': geojson
        }
            
    scene_filter = {
        'acquisitionFilter': acquisition_filter,
        'cloudCoverFilter': cloud_cover_filter,
        'datasetName': dataset,
        'ingestFilter': ingest_filter,
        'metadataFilter': metadata_filter,
        'seasonalFilter': seasonal_filter,
        'spatialFilter': spatial_filter
    }
    
    payload = { 
        'datasetName': dataset,
        'maxResults': max_results,
        'startingNumber': 1, 
        'sceneFilter': scene_filter
    }
    
    matching_scenes = send_request(url + "scene-search", payload, api_key, verbose=verbose)['results']
    entityIds = []
    for scene in matching_scenes:
        entityIds.append(scene['entityId'])
    
    # add scenes to list
    list_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = {
        'listId': list_id,
        'entityIds': entityIds,
        'datasetName': dataset
    }
    
    num_scenes = send_request(url + "scene-list-add", payload, api_key, verbose=verbose)   
    
    # get download options
    payload = {
        'listId': list_id,
        'datasetName': dataset
    }
    
    products = send_request(url + "download-options", payload, api_key, verbose=verbose)
    downloads = []
    for product in products:
        if product['bulkAvailable']:         
            downloads.append({'entityId':product['entityId'], 'productId':product['id']})
    
    # remove the list
    payload = {
        'listId': list_id
    }
    
    send_request(url + "scene-list-remove", payload, api_key, verbose=verbose) 
    
    return downloads


class JSONFormatError(Exception):
    pass


""" Translate geojson into a format that the API understands. """
def format_geojson(geojson):
    geojson = dict(json.load(geojson))  
    try:
        if geojson['type'] == 'FeatureCollection':
            if len(geojson['features']) != 1:
                raise JSONFormatError(("Unsupported number of GeoJSON features: {len(geojson['features'])}."))
            geometry = geojson['features'][0]['geometry']
        elif geojson['type'] == 'Feature':
            geometry = geojson['geometry']
        else:
            raise JSONFormatError(f"Unsupported GeoJSON type: {geojson['type']}")

        formatted = {}
        formatted['type'] = geometry['type']
        formatted['coordinates'] = geometry['coordinates']
        return formatted
    except KeyError as e:
        raise JSONFormatError(f"Error parsing JSON: missing {e} field")
        

def download(url, api_key, downloads, completed_list=[], verbose=True):
    if verbose:
        download = input(f"Download all {len(downloads)} scene(s)? (Y/N) ")
        if download.lower() not in {'y', 'yes'}:
            return None
        
    if verbose: print("Requesting scenes for download...")
    label = datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = {
        'downloads': downloads,
        'label': label
    }
    
    results = send_request(url + "download-request", payload, api_key, verbose=verbose)
    
    for result in results['availableDownloads']:       
        threaded_download(result['url'], threads, completed_list, verbose=verbose)
    
    # if downloads not immediately available, poll until they become available
    preparing_dl_count = len(results['preparingDownloads'])
    preparing_dl_ids = []
    if preparing_dl_count > 0:
        if verbose: print("Waiting for all downloads to become available...")
        for result in results['preparingDownloads']:  
            preparing_dl_ids.append(result['downloadId'])
        payload = {"label" : label}   
        while len(preparing_dl_ids) > 0: 
            time.sleep(30)
            results = send_request(url + "download-retrieve", payload, api_key, False, verbose=verbose)
            if results != False:
                for result in results['available']:                            
                    if result['downloadId'] in preparing_dl_ids:
                        preparing_dl_ids.remove(result['downloadId'])
                        threaded_download(result['url'], threads, completed_list, verbose=verbose)
                        
                for result in results['requested']:   
                    if result['downloadId'] in preparing_dl_ids:
                        preparing_dl_ids.remove(result['downloadId'])
                        threaded_download(result['url'], threads, completed_list, verbose=verbose)
                        
    return completed_list


def main():
    parser = argparse.ArgumentParser(
        description="Search for and download L8 scenes that match criteria.")
    
    parser.add_argument("-date-range", "--dr", metavar=("start", "end"), 
                        dest="date_range", nargs=2, type=str,
                        help="filter scenes by acquisition date (format: yyyy-mm-dd yyyy-mm-dd)")
    parser.add_argument("-cloud-range", "--cr", metavar=("min", "max"),
                        dest="cloud_range", nargs=2, type=int,
                        help="filter scenes by cloud cover")
    parser.add_argument("-boundary", "--b", metavar="path/to/geojson",
                        dest="boundary", type=Path,
                        help="path to geojson file with boundary of search")
    parser.add_argument("-dst", metavar="bucket", type=str,
                        help="s3 bucket to store downloaded scenes in")
    parser.add_argument("-maxresults", "--mr", metavar="int",
                        dest="max_results", type=int, default=50,
                        help="max number of results to return from search")
    parser.add_argument("-maxthreads", "--mt", metavar="int",
                        dest="max_threads", type=int, default=5,
                        help="max number of threads to use to download")
    parser.add_argument("-quiet", "--q", dest="verbose", action="store_false",
                        help="suppress printing to the console")
    args = parser.parse_args()
    
    util.sema = threading.Semaphore(value=args.max_threads)
    
    url = "https://m2m.cr.usgs.gov/api/api/json/stable/"
    dataset = "landsat_ot_c2_l2"
    dataset_name = "landsat"
    
    api_key = authenticate(url, verbose=args.verbose)
    
    downloads = search(url, api_key, dataset, 
                       max_results=args.max_results, 
                       date_range=args.date_range, 
                       cloud_range=args.cloud_range, 
                       boundary=args.boundary,
                       verbose=args.verbose)
    
    completed_list = []
    download(url, api_key, downloads, completed_list, verbose=args.verbose)
    
    # wait until all downloads have finished
    for thread in threads:
        while thread.is_alive():
            time.sleep(30)
    
    # execute lambda function locally for testing
    for file in completed_list:
        file = f"/vsitar/{file}"
        result = calc_ndvi_and_mask_l8_clouds(file)
        if args.verbose: print(f"Generated {result}")
        
    # upload files to s3 if bucket is specified
    if args.dst:
        # assuming you have set up aws credentials properly
        # TODO: add command line argument to change profile?
        s3 = boto3.resource('s3')
        # a full breakdown of the naming convention can be found here:
        # https://www.usgs.gov/faqs/what-naming-convention-landsat-collection-2-level-1-and-level-2-scenes?qt-news_science_products=0#qt-news_science_products
        l8_name_pattern = re.compile(r"""
                        (?P<sat>L\w{3})         # match the sensor type and satellite (ex. LC08)
                        (?:_)
                        (?P<level>L\w{3})       # match the processing level (ex. L2SP)
                        (?:_)
                        (?P<path>\d{3})         # match the path
                        (?P<row>\d{3})          # match the row
                        (?:_)
                        (?P<acq_year>\d{4})     # match the acquisition year
                        (?P<acq_month>\d{2})    # match the acquisition month
                        (?P<acq_day>\d{2})      # match the acquisition day
                        (?:_)
                        (?P<proc_year>\d{8})    # match the processing year
                        (?:_)
                        (?P<col_number>\d{2})   # match the collection number
                        (?:_)
                        (?P<col_category>\w{2}) # match the collection category
                        (?:.tar)
                        """, re.VERBOSE)
        for file in completed_list:
            m = l8_name_pattern.match(file)
            prefix = s3_join(dataset_name, m.group('path'), m.group('row'), m.group('acq_year'),
                             m.group('acq_month'))
            upload_to_s3(file, args.dst, prefix, s3, delete=True, verbose=args.verbose)
            
    if args.verbose: print("Done.")
    

if __name__ == "__main__":
    main()