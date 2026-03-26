import requests, os, time, sys, json
from dotenv import load_dotenv
load_dotenv()
from dynamov2.logger.logger import CustomLogger
from dynamov2.database.db_helper import db_helper
from libs.github.compose_finder import get_docker_compose_filepaths

'''
Relevant Rate limit:

The code_search object provides your rate limit status for the REST API for searching code.

    "code_search": {
      "limit": 10,
      "used": 0,
      "remaining": 10,
      "reset": 1691591091
    }

The core object provides your rate limit status for all non-search-related resources in the REST API.

    "core": {
      "limit": 5000,
      "used": 1,
      "remaining": 4999,
      "reset": 1691591363
    }
'''

PROGRESS_FILE = os.path.join(os.path.dirname(__file__), '.github_search_progress.json')

def save_progress(start_size, end_size, page):
    """Save current progress to file for resuming after crashes."""
    progress = {
        'start_size': start_size,
        'end_size': end_size,
        'page': page
    }
    with open(PROGRESS_FILE, 'w') as f:
        json.dump(progress, f)

def load_progress():
    """Load progress from file if it exists, otherwise return defaults."""
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, 'r') as f:
                progress = json.load(f)
            return progress['start_size'], progress['end_size'], progress['page']
        except (json.JSONDecodeError, KeyError):
            pass
    return 40, 40, 1  # Default values

def clear_progress():
    """Remove progress file when search is complete."""
    if os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)


def safe_get_docker_compose_filepaths(fullname, token, logger, max_retries=3):
    """Call `get_docker_compose_filepaths` with retry/backoff for transient errors.

    Returns an empty list on permanent failure.
    """
    backoff = 1
    for attempt in range(1, max_retries + 1):
        try:
            return get_docker_compose_filepaths(fullname, token, logger)
        except Exception as e:
            if logger:
                logger.warning(f"Error fetching compose paths for {fullname}: {e} (attempt {attempt}/{max_retries})")
            if attempt < max_retries:
                time.sleep(backoff)
                backoff *= 2
                continue
            return []

if __name__ == '__main__':
    
    AUTH_TOKEN = os.getenv("GITHUB_TOKEN")
    logger1 = CustomLogger('GitHub Search')

    url = "https://api.github.com/"
    search_api_endpoint = "search/code"

    # Load progress from file if resuming, otherwise use defaults
    start_size, end_size, saved_page = load_progress()
    SIZE_DIFF = 1
    query = 'docker-compose in:path'
    new_query = query + f" size:{start_size}..{end_size}"

    logger1.info(f"Starting/Resuming search from size:{start_size}..{end_size}, page:{saved_page}")

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {AUTH_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    params = {'q': new_query,
            'per_page': 100,
            'page': saved_page}

    while start_size < 923:
        search_results = requests.get(url+search_api_endpoint, headers=headers, params=params)
        if search_results.ok != True:
            if search_results.headers.get("X-RateLimit-Remaining",0) == '0':
                sleep_time = int(search_results.headers.get("X-RateLimit-Reset",0)) - int(time.time())
                if sleep_time > 0:
                    logger1.info(f"Rate limit hit. Sleeping for {sleep_time}")
                    time.sleep(sleep_time)
                else:
                    logger1.info("Rate limit reset time already passed; continuing without sleeping")
            # elif search_results.status_code == 422: #422 error means there is more than 1000 results and the code is trying to enter page 11
            #     if end_size-5 > start_size:
            #         end_size = end_size-5  #Try to reduce by 5 to see if it helps
            #     else:
            #         start_size += 1 #If the above doesnt work, we will need to drop some entries by increasing start_size.
            else:
                logger1.warning(f"Error {search_results.status_code}: {search_results.text}")
                break
        else:
            search_results = search_results.json()
            # Cap total_count at GitHub Search API hard limit (1000) and process up to 10 pages
            total_count = int(search_results.get('total_count', 0))
            effective_total = min(total_count, 1000)
            per_page = int(params.get('per_page', 100))

            logger1.info(f"Search return count: {total_count}: Page {params['page']} from {int(start_size)}..{int(end_size)} ")
            if search_results.get("items") and params['page'] <= 10:
                for d in search_results["items"]:
                        github_url = d["repository"]["html_url"]
                        '''
                        Deal with githubs with IP allow lists that is not private..?
                        '''
                        if 'corelogic-private' in github_url or 'jiko-auth' in github_url:
                            continue

                        fullname = d["repository"]["full_name"]
                        repository = db_helper.get_github_repository(url=github_url)
                        search_path = d["path"]
                        if not repository:
                            filename = d["name"]
                            if "docker" in filename and "compose" in filename and ".github" not in search_path and ".travis" not in search_path :
                                '''
                                Ignoring .github, .travis paths results in ignoring CI/CD
                                '''
                                stars_endpoint = f"repos/{fullname}"
                                commit_api_endpoint = f"repos/{fullname}/commits?per_page=1"
                                last_commit_date = requests.get(url=url+commit_api_endpoint,headers=headers)
                                while last_commit_date.ok != True: #Check for rate limiting issues
                                    if last_commit_date.headers.get("X-RateLimit-Remaining",0) == "0":
                                        sleep_time = int(last_commit_date.headers.get("X-RateLimit-Reset",0)) - int(time.time())
                                        if sleep_time > 0:
                                            logger1.info(f"Rate limit hit. Sleeping for {sleep_time}")
                                            time.sleep(sleep_time)
                                        else:
                                            logger1.info("Rate limit reset time already passed; continuing without sleeping")
                                        last_commit_date = requests.get(url=url+commit_api_endpoint,headers=headers)
                                    else:
                                        logger1.warning(f"Error {last_commit_date.status_code}: {last_commit_date.text}")
                                        print(d)
                                        sys.exit()
                                last_commit_date = last_commit_date.json()[0]["commit"]["author"]["date"]
                                repository_information = requests.get(url=url+stars_endpoint,headers=headers)
                                while repository_information.ok != True: #Check for rate limiting issues
                                    if repository_information.headers.get("X-RateLimit-Remaining",0) == "0":
                                        sleep_time = int(repository_information.headers.get("X-RateLimit-Reset",0)) - int(time.time())
                                        if sleep_time > 0:
                                            logger1.info(f"Rate limit hit. Sleeping for {sleep_time}")
                                            time.sleep(sleep_time)
                                        else:
                                            logger1.info("Rate limit reset time already passed; continuing without sleeping")
                                        repository_information = requests.get(url=url+stars_endpoint,headers=headers)
                                    else:
                                        logger1.warning(f"Error {repository_information.status_code}: {repository_information.text}")
                                        sys.exit()
                                repository_information = repository_information.json()
                                stars_number = repository_information["stargazers_count"]
                                created_at = repository_information["created_at"]
                                number_of_issues = repository_information["open_issues_count"]
                                about = repository_information["description"]
                                readme_endpoint = f"repos/{fullname}/readme"
                                readme_data = requests.get(url=url+readme_endpoint,headers=headers)
                                while readme_data.ok != True and readme_data.status_code != 404: #At this point, the repository should exist. The README might not so check for 404
                                    if readme_data.headers.get("X-RateLimit-Remaining",0) == "0": #Deal with rate limiting errors
                                        sleep_time = int(readme_data.headers.get("X-RateLimit-Reset",0)) - int(time.time())
                                        if sleep_time > 0:
                                            logger1.info(f"Rate limit hit. Sleeping for {sleep_time}")
                                            time.sleep(sleep_time)
                                        else:
                                            logger1.info("Rate limit reset time already passed; continuing without sleeping")
                                        readme_data = requests.get(url=url+stars_endpoint,headers=headers)
                                    else:
                                        logger1.warning(f"Error when processing {fullname} {readme_data.status_code}: {readme_data.text}")
                                        logger1.warning(f"{d}")
                                        sys.exit()
                                readme_data = readme_data.json()
                                has_readme = bool(readme_data.get("content")) if isinstance(readme_data, dict) else False
                                compose_paths = safe_get_docker_compose_filepaths(fullname, AUTH_TOKEN, logger1)
                                # Only add repository if compose files were found.
                                if compose_paths:
                                    db_helper.add_github_repository(
                                        name=fullname,
                                        url=github_url,
                                        about=about,
                                        created_at=created_at,
                                        last_commit=last_commit_date,
                                        num_stars=stars_number,
                                        num_issues=number_of_issues,
                                        readme=has_readme,
                                        docker_compose_filepath=compose_paths,
                                        cleaned_docker_compose_filepath=compose_paths
                                    )
                                else:
                                    logger1.info(f"Skipping {fullname} - no docker-compose files found")
                            else:
                                logger1.info(f"Dropped {fullname}, {github_url}. Filename: {filename}.")
                        if repository:
                            compose_paths = safe_get_docker_compose_filepaths(fullname, AUTH_TOKEN, logger1)
                            if compose_paths:
                                db_helper.add_github_repository(
                                    url=github_url,
                                    name=fullname,
                                    docker_compose_filepath=compose_paths,
                                    cleaned_docker_compose_filepath=compose_paths
                                )
                            else:
                                logger1.info(f"Not updating {fullname} - no docker-compose files found")
                params['page'] += 1
                save_progress(start_size, end_size, params['page'])
            # If we've exhausted the effective results for this query (up to 1000), or no items, or reached page limit, move to next size
            if params['page'] * per_page > effective_total or not search_results.get("items") or params['page'] >= 10:
                start_size += SIZE_DIFF
                end_size += SIZE_DIFF
                new_query = query + f" size:{start_size}..{end_size}"
                params['q'] = new_query
                params['page'] = 1
                save_progress(start_size, end_size, params['page'])