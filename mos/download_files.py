#!/usr/bin/env python3

import itertools
import logging
import os
import requests
import sys
import time
import traceback
import uuid

from ast import literal_eval
from bs4 import BeautifulSoup as bs

try:
    from pathvalidate import sanitize_filename
except ModuleNotFoundError:

    def sanitize_filename(path):
        return "".join([_ for _ in path if _.isalnum()])


try:
    from mos.encryption_routine import encryption_routine
    from mos.headers import csrf_request_headers, download_image_headers
except ModuleNotFoundError:
    from encryption_routine import encryption_routine
    from headers import csrf_request_headers, download_image_headers


class Downloader:
    def __init__(self, record_URL, base_images_dir, args=None):
        self.session = requests.Session()
        self.record_URL = record_URL
        self.base_images_dir = base_images_dir

        self.file_range = None
        self.deep_hierarchy = False
        self.archive_directory_name = None
        self.image_URLs_and_labels = None
        self.csrf_token = None
        self.CRAWL_SPEED = 2  # 2 second delay between each archive request
        self.skip_existing = False
        self.include_fullname = False
        self.simple_dirnames = False

        if args:
            self.file_range = args.range
            self.deep_hierarchy = args.deep
            if args.crawl_speed and args.crawl_speed > 0:
                self.CRAWL_SPEED = args.crawl_speed
            self.skip_existing = args.skip_existing
            self.include_fullname = args.include_fullname
            self.simple_dirnames = args.simple_dirnames

    @classmethod
    def log_error_and_exit(cls, error_message):
        logging.error(error_message)
        sys.exit()

    @staticmethod
    def is_registers_url(url):
        try:
            response = requests.Session().get(url, headers=csrf_request_headers())
            soup = bs(response.text, "html.parser")
            register_header = soup.find("h3", {"id": "register-header"})
            return register_header is not None
        except Exception:
            return False

    def create_archive_directory(self):
        archive_directory_path = os.path.join(
            self.base_images_dir, self.archive_directory_name
        )
        archive_directory_path = os.path.normpath(archive_directory_path)
        os.makedirs(archive_directory_path, exist_ok=True)

    def fetch_registers_page_and_download_all(self):
        try:
            # get first registers page to determine how many pages to parse
            response = self.session.get(self.record_URL, headers=csrf_request_headers())
            if response.status_code != 200:
                self.log_error_and_exit(
                    f"{response.status_code} Status Code Fetching CSRF Token, Exiting"
                )
                return
            registers_pages = list(self.registers_page_parse_list_pages(response.text))
            logging.info(
                f"Fetched list of {len(registers_pages)} register pages on: {self.record_URL}"
            )

            # get registers page to determine how many pages to parse
            def record_urls_from_page(base_url, page):
                url = f"{base_url}?page={page}"
                response = self.session.get(url, headers=csrf_request_headers())
                if response.status_code != 200:
                    self.log_error_and_exit(
                        f"{response.status_code} Status Code Fetching CSRF Token, Exiting"
                    )
                    return
                record_URLs = list(self.registers_page_parse(response.text))
                logging.info(
                    f"Fetched {len(record_URLs)} archive urls on '{url}' [{page}/{len(registers_pages)}]"
                )
                yield from record_URLs

            record_urls_gens = (
                record_urls_from_page(self.record_URL, page) for page in registers_pages
            )
            record_urls = list(itertools.chain.from_iterable(record_urls_gens))
            logging.info(f"Found total of {len(record_urls)} archive URLs")
            logging.debug("Archive URLs:" + "\n".join(record_urls))

            # download all records
            for record_URL in record_urls:
                self.record_URL = record_URL
                self.fetch_record_page()
                self.download_files()
        except Exception:
            self.log_error_and_exit(
                f"Unexpected Error Fetching and Parsing Registers Page, Exiting\n{traceback.format_exc()}"
            )

    def fetch_record_page(self):
        try:
            response = self.session.get(self.record_URL, headers=csrf_request_headers())
            if response.status_code == 200:
                self.get_csrf_token()
                self.parse_image_URLs_and_labels(response.text)
                self.parse_archive_name(response.text)
                self.create_archive_directory()
            else:
                self.log_error_and_exit(
                    f"{response.status_code} Status Code Fetching CSRF Token, Exiting"
                )
        except Exception:
            self.log_error_and_exit(
                f"Unexpected Error Fetching Record Page, Exiting\n{traceback.format_exc()}"
            )

    def get_csrf_token(self):
        csrf_token = self.session.cookies.get_dict().get("shared_csrftoken")

        if csrf_token is not None:
            logging.info("Fetched CSRF")
            self.csrf_token = csrf_token
        else:
            self.log_error_and_exit("CSRF Token Not Received, Exiting")

    def registers_page_parse_list_pages(self, response_text):
        try:
            soup = bs(response_text, "html.parser")

            # there might be multiple pages
            register_header = soup.find("h3", {"id": "register-header"})
            page_links_list = register_header.findNext("ul")
            if page_links_list is None:
                yield 1
                return
            last_page_link = page_links_list.findAll("a", {"class": "page-link"})[-2]
            last_page_no = int(last_page_link.getText())
            pages = (i + 1 for i in range(last_page_no))

            yield from pages
        except Exception:
            self.log_error_and_exit(
                f"Unexpected Error Fetching Registers Page, Exiting\n{traceback.format_exc()}"
            )

    def registers_page_parse(self, response_text):
        try:
            soup = bs(response_text, "html.parser")
            registers_table_rows = soup.find(
                "div", {"class": "table-responsive"}
            ).findAll("tr")
            registers_table_cells = (
                tr.findChildren("td") for tr in registers_table_rows
            )
            registers_name_cells = (
                tds[1] for tds in registers_table_cells if len(tds) > 1
            )
            registers_names = (td.getText() for td in registers_name_cells)
            record_urls = (
                f"{self.record_URL}/{register_name}"
                for register_name in registers_names
            )

            yield from record_urls
        except Exception:
            self.log_error_and_exit(
                f"Unexpected Error Fetching Registers Page, Exiting\n{traceback.format_exc()}"
            )
            return []

    def parse_image_URLs_and_labels(self, record_response_text):
        if (
            '"files":' not in record_response_text
            or "labels" not in record_response_text
        ):
            self.log_error_and_exit("Error Parsing Image URLs, Exiting")

        start_files_list = record_response_text.split('"files":')[1]
        full_files_list = literal_eval(start_files_list.split('"],')[0].strip() + '"]')

        start_labels_list = record_response_text.split('"labels":')[1]
        full_labels_list = literal_eval(
            start_labels_list.split('"],')[0].strip() + '"]'
        )

        self.image_URLs_and_labels = tuple(zip(full_files_list, full_labels_list))

        # if a range is provided and the range is less than the number of scraped files, use it
        if self.file_range is not None and self.file_range < len(
            self.image_URLs_and_labels
        ):
            self.image_URLs_and_labels = self.image_URLs_and_labels[0 : self.file_range]

        logging.info(
            f"Fetched list of {len(self.image_URLs_and_labels)} images from archive '{self.record_URL}'"
        )

    def parse_archive_name(self, record_response_text):
        try:
            if self.simple_dirnames:
                raise Exception(
                    "user requested to not parse HTML-page for output directory names"
                )
            soup = bs(record_response_text, "html.parser")
            register_data = soup.find(
                "table", {"class": "table table-register-data"}
            ).findAll("td")

            archive_category = register_data[0].text.strip()
            archive_id = register_data[1].text.strip()
            if self.include_fullname:
                try:
                    fullname = register_data[2].text.strip()
                    if fullname:
                        archive_id += "__" + fullname
                except:
                    pass

        except Exception as e:
            logging.debug("Fallback to using URL for directory name", exc_info=True)
            try:
                archive_category, archive_id = self.record_URL.strip("/").split("/")[
                    -2:
                ]
            except Exception as e:
                logging.error(
                    "Error parsing Archive category and ID, creating random directory name"
                )
                self.archive_directory_name = str(uuid.uuid4())[
                    0:18
                ]  # first 18 chars of a random UUID
                return
            logging.info(
                "Error parsing Archive category and ID, creating directory name based on URL"
            )

        # removing characters that could cause errors when writing creating dir
        archive_category = sanitize_filename(archive_category)
        archive_id = sanitize_filename(archive_id)

        if self.deep_hierarchy:
            self.archive_directory_name = os.path.join(archive_category, archive_id)
        else:
            self.archive_directory_name = archive_category + "_" + archive_id

    def save_image(self, image_content, file_path, file_number):
        logging.info(
            f"Downloaded '{file_path}' [{file_number}/{len(self.image_URLs_and_labels)}]"
        )
        with open(file_path, "wb") as f:
            f.write(image_content)

    def download_files(self):
        for index, (image_path, image_label) in enumerate(self.image_URLs_and_labels):
            request_attempts = 0
            url = encryption_routine.createValidURL(image_path, self.csrf_token)
            file_number = index + 1
            file_path = os.path.join(
                self.base_images_dir, self.archive_directory_name, f"{image_label}.jpg"
            )
            if self.skip_existing and os.path.exists(file_path):
                logging.info(
                    f"Skip existing '{file_path}' [{file_number}/{len(self.image_URLs_and_labels)}]"
                )
                continue

            while request_attempts < 3:
                try:
                    response = self.session.get(url, headers=download_image_headers())
                    if response.status_code == 200:
                        self.save_image(response.content, file_path, file_number)
                    else:
                        logging.info(
                            f"Skipping file {file_number} ({response.status_code} Response)"
                        )
                    time.sleep(self.CRAWL_SPEED)
                    break
                except requests.exceptions.ConnectionError:
                    if request_attempts < 3:
                        request_attempts += 1
                        logging.info(
                            f"Retrying file {file_number}, Attempt ({request_attempts})"
                        )
                    else:
                        logging.info(
                            f"Skipping file {file_number}, {request_attempts} failed attempts"
                        )
                except Exception:
                    logging.info(
                        f"Skipping file {file_number}\n ({traceback.format_exc()})"
                    )
                    break


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(message)s",
        level=logging.INFO,
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    download = Downloader(
        "https://data.matricula-online.eu/en/deutschland/akmb/militaerkirchenbuecher/0002/?pg=1",
        "./images",
        None,
    )
    download.fetch_record_page()
    download.download_files()
