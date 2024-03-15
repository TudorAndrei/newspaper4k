# -*- coding: utf-8 -*-
# Much of the code here was forked from https://github.com/codelucas/newspaper
# Copyright (c) Lucas Ou-Yang (codelucas)
"""Module providing the Article class for newspaper. The Article class
abstracts the concept of a news article, providing methods and properties
to download, parse and analyze said article.
"""

from datetime import datetime
import json
import logging
import copy
from typing import Any, Dict, List, Literal, Optional, Set, Union, overload
from urllib.parse import urlparse
import lxml
from readability import Document
import requests

from newspaper.exceptions import ArticleException
from newspaper.text import StopWords

from . import network
from . import nlp
from . import settings
from . import urls

from .cleaners import DocumentCleaner
from .configuration import Configuration
import newspaper.parsers as parsers
from .extractors import ContentExtractor
from .outputformatters import OutputFormatter
from .utils import (
    get_available_languages,
    extract_meta_refresh,
)

log = logging.getLogger(__name__)

available_requests_params = [
    "headers",
    "cookies",
    "auth",
    "timeout",
    "allow_redirects",
    "proxies",
    "verify",
    "cert",
]


class ArticleDownloadState:
    """Download state for the Article object."""

    NOT_STARTED = 0
    FAILED_RESPONSE = 1
    SUCCESS = 2


class Article:
    """Article abstraction for newspaper.

    This object fetches and holds information for a single article.
    In order to download the article, call `download()`. Then call `parse()`
    to extract the information.

    Attributes:
        config (Configuration): the active configuration for this article instance.
            You can use different settings for any article instance.
        extractor (ContentExtractor): Content parsing object.
        source_url (str): URL to the main page of the news source which
            owns this article
        url (str): The article link. This was used to download the current
            article. In case of a redirect(through meta refresh or read
            more link), this will be different from the original url.
        original_url (str): The original url of the article. This is the url
            that was passed to the constructor. It will not change in
            case of a redirect.
        title (str): Parset title of the article. It can be forced/overridden
            by providing a title in the constructor.
        read_more_link (str): An xpath selector for the link to the full
            article. make sure that the selector works for all casese, not
            only for one specific article. If needed, you can use several xpath
            selectors separated by ``|``.
        top_image (str): The top image url of the article. It will try to guess
            the best fit for a main image from the images found in the article.
        meta_img (str): Image url provided by metadata
        images (List[str]): List of all image urls in the current article
        movies (List[str]): List of video links in the article body
        text (str): a parsed version of the article body. It will be truncated
            to the first `config.max_text` characters.
        text_cleaned (str): a parsed version of the clean_top_node content.
            It will be truncated to the first `config.max_text` characters.
        keywords (List[str]): An inferred list of keywords for this article.
            This will be generated by the nlp method. It will be truncated to
            the first `config.max_keywords` keywords.
        keyword_scores (Dict[str, float]): A dictionary of keywords and their
            scores.
        meta_keywords (List[str]):  A list of keywords provided by the meta data.
            It will be truncated to the first `config.max_keywords` keywords.
        tags (Set[str]): Extracted tag list from the article body
        authors (List[str]): The author list parsed from the article. It will
            be truncated to the first `config.max_authors` authors.
        publish_date (str): The parsed publishing date from the article. If no
            valid date is found, it will be an empty string.
        summary (str): The summarization of the article as generated by the nlp
            method. It will be truncated to the first `config.max_summary_sent`
            sentences.
        html (str): The raw html of the article page.
        article_html (str): The raw html of the article body.
        is_parsed (bool): True if parse() has been called.
        download_state (int): AticleDownloadState.SUCCESS if `download()` was
            successful, ArticleDownloadState.FAILED_RESPONSE if `download()` failed,
            `ArticleDownloadState.NOT_STARTED` if `download()` was not called.
        download_exception_msg (str): The exception message if download() failed.
        history (List[str]): Redirection history from the requests.get call.
        meta_description (str): The description extracted from the meta data.
        meta_lang (str): The language extracted from the meta data.
            If config.language is not set, this value will be used
            to parse the article instead of the config.language value.
        meta_favicon (str): Website's favicon url extracted from the meta data.
        meta_site_name (str): Website's name extracted from the meta data.
        meta_data (Dict[str, str]): additional meta data extracted from
            the meta tags.
        canonical_link (str): Canonical URL for the article extracted from the metadata
        top_node (lxml.html.HtmlElement): Top node of the original DOM tree.
            It contains the text nodes for the detected article body. This node
            is on the doc DOM tree.
        clean_top_node (lxml.html.HtmlElement): Top node for the article on the
            cleaned version of the DOM. This node is _not_ in the doc DOM tree.
        doc (lxml.html.HtmlElement): the full DOM of the downloaded html. It is
            the original DOM tree.
        clean_doc (lxml.html.HtmlElement): a cleaned version of the DOM tree


    """

    def __init__(
        self,
        url: str,
        title: str = "",
        source_url: str = "",
        read_more_link: str = "",
        config: Optional[Configuration] = None,
        **kwargs: Any,
    ):
        """Constructs the article class. Will not download or parse the article

        Args:
            url (str): The input url to parse. Can be a URL or a file path.
            title (str, optional): Default title if none can be
                extracted from the webpage. Defaults to "".
            source_url (str, optional): URL of the main website that
                originates the article.
                If left empty, it will be inferred from the url. Defaults to "".
            read_more_link (str, optional): A xpath selector for the link to the
                full article, in case there is a 'preview' with a read-more
                button that leads to another url (even on another domain).
                make sure that the selector works for all cases,
                not only for one specific article. If needed, you can use
                several xpath selectors separated by `|`. Defaults to "".
            config (Configuration, optional): Configuration settings for
            this article's download/parsing/nlp. If left empty, it will
            use the default settingsDefaults to None.

        Keyword Args:
            **kwargs: Any Configuration class property can be overwritten
                    through init keyword params.
                    Additionally, you can specify any of the following
                    requests parameters:
                    headers, cookies, auth, timeout, allow_redirects,
                    proxies, verify, cert

        Raises:
            ArticleException: Error parsing and preparing the article
        """
        if isinstance(title, Configuration) or isinstance(source_url, Configuration):
            raise ArticleException(
                "Configuration object being passed incorrectly as title or "
                "source_url! Please verify `Article`s __init__() fn."
            )

        self.config: Configuration = config or Configuration()
        # Set requests parameters. These are passed directly to requests.get
        for k in available_requests_params:
            if k in kwargs:
                self.config.requests_params[k] = kwargs[k]
                del kwargs[k]
        self.config.update(**kwargs)

        self.extractor = ContentExtractor(self.config)

        if source_url == "":
            scheme = urls.get_scheme(url)
            if scheme is None:
                scheme = "http"
            source_url = scheme + "://" + urls.get_domain(url)

        if source_url is None or source_url == "":
            raise ArticleException("input url bad format")

        # URL to the main page of the news source which owns this article
        self.source_url = source_url

        self.url = urls.prepare_url(url, self.source_url)

        # In case of follow read more link, we need to keep the original url
        self.original_url = self.url

        self._title = title
        self.title = title

        # An xpath that allows to find the link to the full article
        self.read_more_link = read_more_link

        # URL of the "best image" to represent this article
        self.top_image = ""

        # stores image provided by metadata
        self.meta_img = ""

        # All image urls in this article
        self.images: List[str] = []

        # All videos in this article: youtube, vimeo, etc
        self.movies: List[str] = []

        # Body text from this article
        self._text = ""
        self.text_cleaned = ""

        # `keywords` are extracted via nlp() from the body text
        self.keywords: List[str] = []

        # `keyword_scores` a dictionary of keywords and their scores
        self.keyword_scores: Dict[str, float] = {}

        # `meta_keywords` are extracted via parse() from <meta> tags
        self.meta_keywords: List[str] = []

        # `tags` are also extracted via parse() from <meta> tags
        self.tags: Set[str] = set()

        # List of authors who have published the article, via parse()
        self.authors: List[str] = []

        self.publish_date: Optional[datetime] = None

        # Summary generated from the article's body txt
        self._summary = ""

        # This article's unchanged and raw HTML
        self._html = ""

        # The HTML of this article's main node (most important part)
        self.article_html = ""

        # Keep state for downloads and parsing
        self.is_parsed = False
        self.download_state = ArticleDownloadState.NOT_STARTED
        self.download_exception_msg: Optional[str] = None

        # Redirection history from the requests.get call
        self.history: Optional[List[str]] = []

        # Meta description field in the HTML source
        self.meta_description = ""

        # Meta language field in HTML source
        self.meta_lang = ""

        # Meta favicon field in HTML source
        self.meta_favicon = ""

        # Meta site_name field in HTML source
        self.meta_site_name = ""

        # Meta tags contain a lot of structured data, e.g. OpenGraph
        self.meta_data: Dict[str, str] = {}

        # The canonical link of this article if found in the meta data
        self.canonical_link = ""

        # Holds the top element of the DOM that we determine is a candidate
        # for the main body of the article
        self.top_node: Optional[lxml.html.Element] = None

        # A deepcopied clone of the above object before heavy parsing
        # operations, useful for users to query data in the
        # "most important part of the page"
        self.clean_top_node: Optional[lxml.html.Element] = None

        # The top node complemented with siblings (off-tree)
        self._top_node_complemented: Optional[lxml.html.Element] = None

        # lxml DOM object generated from HTML
        self.doc: Optional[lxml.html.Element] = None

        # A deepcopied clone of the above object before undergoing heavy
        # cleaning operations, serves as an API if users need to query the DOM
        self.clean_doc: Optional[lxml.html.Element] = None

    def build(self):
        """Build a lone article from a URL independent of the source (newspaper).
        Don't normally call this method b/c it's good to multithread articles
        on a source (newspaper) level.
        Calls download(), parse(), and nlp() in succession.
        """
        self.download()
        self.parse()
        self.nlp()

    def _parse_scheme_file(self, path):
        try:
            with open(path, "r", encoding="utf-8") as fin:
                return fin.read()
        except OSError as e:
            self.download_state = ArticleDownloadState.FAILED_RESPONSE
            self.download_exception_msg = e.strerror
            return None

    def _parse_scheme_http(self, url: Optional[str] = None):
        try:
            # We do not use get_html() here because we want to be able to
            # detect protection in the response regardless of the status code
            html, status_code, history = network.get_html_status(
                url or self.url, self.config
            )
            self.history = [r.url for r in history]
            if status_code >= 400:
                self.download_state = ArticleDownloadState.FAILED_RESPONSE
                protection = self._detect_protection(html)
                if protection:
                    self.download_exception_msg = (
                        f"Website protected with {protection}, url: {url}"
                    )
                else:
                    self.download_exception_msg = (
                        f"Status code {status_code} for url {url}"
                    )
                return None
        except requests.exceptions.RequestException as e:
            self.download_state = ArticleDownloadState.FAILED_RESPONSE
            self.download_exception_msg = str(e)
            return None

        return html

    def _detect_protection(self, html):
        if "cloudflare" in html:
            return "Cloudflare"
        if "/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page" in html:
            return "Cloudflare"
        if "cloud-flare" in html:
            return "Cloudflare"
        if "CloudFront" in html:
            return "CloudFront"
        if "perimeterx" in html:
            return "PerimeterX"

        return None

    def download(
        self,
        input_html: Optional[str] = None,
        title: Optional[str] = None,
        recursion_counter: int = 0,
        ignore_read_more: bool = False,
    ) -> "Article":
        """Downloads the link's HTML content, don't use if you are batch async
        downloading articles

        Args:
            input_html (str, optional): A cached version of the article to parse.
                It will load the html from this string without attempting to access
                the article url. If you have a read_more_link xpath
                set up in the constructor, and do not set ignore_read_more to true,
                it will attempt to follow the found read_more link (if any).
                Defaults to None.
            title (str, optional): Force an article title. Defaults to None.
            recursion_counter (int, optional): Used to prevent infinite recursions
            due to meta_refresh. Defaults to 0.
            ignore_read_more (bool, optional): If true, the download process will
            ignore any kind of "read_more" xpath set up in the constructor.
            Defaults to False.
        Returns:
            Article: self
        """

        if input_html is None:
            parsed_url = urlparse(self.url)
            if parsed_url.scheme == "file":
                html = self._parse_scheme_file(parsed_url.path)
            else:
                html = self._parse_scheme_http()
            if html is None:
                log.debug(
                    "Download failed on URL %s because of %s",
                    self.url,
                    self.download_exception_msg,
                )
                return self
        else:
            html = input_html

        if self.config.follow_meta_refresh:
            meta_refresh_url = extract_meta_refresh(html)
            if meta_refresh_url and recursion_counter < 1:
                return self.download(
                    input_html=network.get_html(meta_refresh_url),
                    recursion_counter=recursion_counter + 1,
                )

        if not ignore_read_more and self.read_more_link:
            doc = parsers.fromstring(html)
            for read_more_node in doc.xpath(self.read_more_link):
                # TODO: add check for onclick redirections. need some examples
                if read_more_node.get("href"):
                    new_url = read_more_node.get("href")
                    log.info(
                        "After downloading %s, found read more link: %s",
                        self.url,
                        new_url,
                    )
                    new_url = urls.prepare_url(new_url, self.url)
                    html_ = self._parse_scheme_http(new_url)
                    if html_ is not None:
                        html = html_
                        self.url = new_url
                        log.info(
                            "Downloaded read more link: %s and updated url to %s",
                            new_url,
                            self.url,
                        )
                    else:
                        log.info(
                            "Failed to download read more link: %s, leaving original"
                            " content in place",
                            new_url,
                        )
                    break

        self.html = html
        if title is not None:
            self.title = title

        return self

    def parse(self) -> "Article":
        """Parse the previously downloaded article.
        If `download()` wasn't called, it will raise
        a `ArticleException` exception.
        Populates the article properties such as:
        ``title``, ``authors``, ``publish_date``,
        ``text``, ``top_image``, etc.
        Returns:
            Article: self
        """
        self.throw_if_not_downloaded_verbose()

        self.doc = parsers.fromstring(self.html)
        self.clean_doc = copy.deepcopy(self.doc)

        if self.doc is None:
            # `parse` call failed, return nothing
            self.is_parsed = True
            return self

        document_cleaner = DocumentCleaner(self.config)
        output_formatter = OutputFormatter(self.config)

        title = self.extractor.get_title(self.clean_doc)
        self.title = title

        authors = self.extractor.get_authors(self.clean_doc)
        self.authors = authors[: self.config.max_authors]

        metadata = self.extractor.get_metadata(self.url, self.clean_doc)
        if metadata["language"] in get_available_languages():
            self.meta_lang = metadata["language"]
            if self.config.use_meta_language:
                self.config.language = metadata["language"]

        self.meta_site_name = metadata["site_name"]
        self.meta_description = metadata["description"]
        self.canonical_link = metadata["canonical_link"]
        self.meta_keywords = metadata["keywords"]
        self.tags = metadata["tags"]
        self.meta_data = metadata["data"]

        self.publish_date = self.extractor.get_publishing_date(self.url, self.clean_doc)

        doc = Document(self.html)
        html_readability = doc.summary()
        self.doc = parsers.fromstring(html_readability)

        # Before any computations on the body, clean DOM object
        self.clean_doc = document_cleaner.clean(self.clean_doc)

        # Top node in the original documentDOM
        self.top_node = self.extractor.calculate_best_node(self.doc)
        # Off-tree Node containing the top node and any relevant siblings
        self._top_node_complemented = self.extractor.top_node_complemented

        # Top node in the cleaned version of the DOM
        self.clean_top_node = self.extractor.calculate_best_node(self.clean_doc)

        self.set_movies(self.extractor.get_videos(self.doc, self.top_node))

        self.fetch_images()

        if self.top_node is not None:
            self._top_node_complemented = document_cleaner.clean(
                self._top_node_complemented
            )
            text, article_html = output_formatter.get_formatted(
                self._top_node_complemented, title
            )
            self.article_html = article_html
            self.text = text

            text, _ = output_formatter.get_formatted(self.clean_top_node, title)
            self.text_cleaned = text[: self.config.max_text] if text else ""

        self.is_parsed = True
        return self

    def fetch_images(self):
        """Fetch top image, meta image and image list from
        current cleaned_doc. Will set the attributes: meta_img,
        top_image, images, meta_favicon
        """
        # TODO: check weather doc or clean doc is better
        # TODO: rewrite set_reddit_top_img. I removed it for now
        self.extractor.parse_images(self.url, self.clean_doc, self.clean_top_node)

        self.meta_img = self.extractor.image_extractor.meta_image
        self.top_image = self.extractor.image_extractor.top_image
        self.images = self.extractor.image_extractor.images
        self.meta_favicon = self.extractor.image_extractor.favicon

    def is_valid_url(self):
        """Performs a check on the url of this link to determine if article
        is a real news article or not
        """
        return urls.valid_url(self.url)

    def is_valid_body(self):
        """If the article's body text is long enough to meet
        standard article requirements, keep the article
        """
        if not self.is_parsed:
            raise ArticleException(
                "must parse article before checking                                    "
                " if it's body is valid!"
            )
        meta_type = self.extractor.metadata_extractor.meta_data["type"]
        wordcount = self.text.split(" ")
        sentcount = self.text.split(".")

        if meta_type == "article" and len(wordcount) > (self.config.min_word_count):
            log.debug("%s verified for article and wc", self.url)
            return True

        if not self.is_media_news() and not self.text:
            log.debug("%s caught for no media no text", self.url)
            return False

        if self.title is None or len(self.title.split(" ")) < 2:
            log.debug("%s caught for bad title", self.url)
            return False

        if len(wordcount) < self.config.min_word_count:
            log.debug("%s caught for word cnt", self.url)
            return False

        if len(sentcount) < self.config.min_sent_count:
            log.debug("%s caught for sent cnt", self.url)
            return False

        if self.html is None or self.html == "":
            log.debug("%s caught for no html", self.url)
            return False

        log.debug("%s verified for default true", self.url)
        return True

    def is_media_news(self):
        """If the article is related heavily to media:
        gallery, video, big pictures, etc
        """
        safe_urls = [
            "/video",
            "/slide",
            "/gallery",
            "/powerpoint",
            "/fashion",
            "/glamour",
            "/cloth",
        ]
        for s in safe_urls:
            if s in self.url:
                return True
        return False

    def nlp(self):
        """Method expects `download()` and `parse()` to have been run.
        It will perform the keyword extraction and summarization"""
        self.throw_if_not_downloaded_verbose()
        self.throw_if_not_parsed_verbose()

        stopwords = StopWords(self.config.language)
        keywords = nlp.keywords(self.text, stopwords, self.config.max_keywords)
        for k, v in nlp.keywords(
            self.title, stopwords, self.config.max_keywords
        ).items():
            if k in keywords:
                keywords[k] += v
                keywords[k] /= 2
            else:
                keywords[k] = v

        keywords = sorted(list(keywords.items()), key=lambda x: x[1], reverse=True)
        keywords = keywords[: self.config.max_keywords]

        self.keywords = [x[0] for x in keywords]  # remove score
        self.keyword_scores = dict(keywords)

        max_sents = self.config.max_summary_sent

        summary_sents = nlp.summarize(
            title=self.title, text=self.text, stopwords=stopwords, max_sents=max_sents
        )
        self.summary = "\n".join(summary_sents)

    @property
    def title(self) -> str:
        return self._title

    @title.setter
    def title(self, value: str):
        self._title = value[: self.config.max_title] if value else ""

    @property
    def text(self) -> str:
        return self._text

    @text.setter
    def text(self, value: str):
        self._text = value[: self.config.max_text] if value else ""

    @property
    def html(self) -> str:
        return self._html

    @html.setter
    def html(self, value: str):
        self.download_state = ArticleDownloadState.SUCCESS
        if value:
            if isinstance(value, bytes):
                value = parsers.get_unicode_html(value)
            self._html = value
        else:
            self._html = ""

    @property
    def imgs(self) -> List[str]:
        """Same as images

        Returns:
            List[str]: list of image urls
        """
        # Seems to be some legacy api,
        return self.images

    @property
    def top_img(self) -> str:
        """Same as top_image

        Returns:
            str: top_image
        """
        # Seems to be some legacy api,
        return self.top_image

    @property
    def summary(self) -> str:
        return self._summary

    @summary.setter
    def summary(self, value: str):
        self._summary = value[: self.config.max_summary] if value else ""

    def set_movies(self, movie_objects):
        """Trim video objects into just urls"""
        movie_urls = [o.src for o in movie_objects if o and o.src]
        self.movies = movie_urls

    def throw_if_not_downloaded_verbose(self):
        """Parse ArticleDownloadState -> log readable status
        -> maybe throw ArticleException
        """
        if self.download_state == ArticleDownloadState.NOT_STARTED:
            raise ArticleException("You must `download()` an article first!")
        elif self.download_state == ArticleDownloadState.FAILED_RESPONSE:
            raise ArticleException(
                "Article `download()` failed with %s on URL %s"
                % (self.download_exception_msg, self.url)
            )

    def throw_if_not_parsed_verbose(self):
        """Parse `is_parsed` status -> log readable status
        -> maybe throw ArticleException
        """
        if not self.is_parsed:
            raise ArticleException("You must `parse()` an article first!")

    @overload
    def to_json(self, as_string: Literal[True]) -> str:
        pass

    @overload
    def to_json(self, as_string: Literal[False]) -> Dict:
        pass

    def to_json(self, as_string: Optional[bool] = True) -> Union[str, Dict]:
        """Create a json string from the article data. It will include the most
        important attributes such as title, text, authors, publish_date, etc.
        Must be called after `parse()`

        Arguments:
            as_string (bool, optional): If True, it will return a json string.
                If False, it will return a json object. Defaults to True.

        Returns:
            str: the json string version of an parsed article.
        """

        self.throw_if_not_parsed_verbose()

        article_dict: Dict[str, Any] = {}

        for metadata in settings.article_json_fields:
            value = getattr(self, metadata, getattr(self.config, metadata, None))
            if isinstance(value, datetime):
                article_dict[metadata] = value.isoformat()
            else:
                article_dict[metadata] = value
        if as_string:
            return json.dumps(article_dict, indent=4, ensure_ascii=False)
        else:
            return article_dict

    def __getstate__(self):
        """Return a pickable object for this article. This can be used for caching"""
        state = self.__dict__.copy()
        # drop non pickable attributes
        if (
            self.download_state == ArticleDownloadState.SUCCESS
            and self.top_node is not None
        ):
            state["__parsed_state"] = True
            self.top_node.set("__newspaper_top_node", "xxx")
            self._top_node_complemented.set("__newspaper_top_node_complemented", "xxx")
            state["_doc_html"] = parsers.node_to_string(self.doc)
        else:
            state["__parsed_state"] = False

        state.pop("extractor", None)
        state.pop("top_node", None)
        state.pop("clean_top_node", None)
        state.pop("_top_node_complemented", None)
        state.pop("doc", None)
        state.pop("clean_doc", None)

        return state

    def __setstate__(self, state):
        """Restore state from the unpickled state"""
        self.__dict__.update(state)
        self.extractor = ContentExtractor(self.config)
        self.top_node = None
        self.clean_top_node = None
        self._top_node_complemented = None
        self.doc = None
        self.clean_doc = None

        if state["__parsed_state"]:
            self.doc = parsers.fromstring(state["_doc_html"])
            delattr(self, "_doc_html")
            nodes = parsers.get_elements_by_attribs(
                self.doc, attribs={"__newspaper_top_node": "xxx"}
            )
            if nodes:
                self.top_node = nodes[0]
            nodes = parsers.get_elements_by_attribs(
                self.doc, attribs={"__newspaper_top_node_complemented": "xxx"}
            )
            if nodes:
                self._top_node_complemented = nodes[0]
        delattr(self, "__parsed_state")

    def __eq__(self, other):
        if not isinstance(other, Article):
            raise NotImplementedError("Can only compare to other Article objects")

        criteria = [
            self.url == other.url,
            self.title == other.title,
            self.text == other.text,
            self.top_image == other.top_image,
            sorted(self.movies) == sorted(other.movies),
            sorted(self.authors) == sorted(other.authors),
            sorted(self.keywords) == sorted(other.keywords),
            sorted(self.images) == sorted(other.images),
            self.publish_date == other.publish_date,
        ]

        return all(criteria)

    def __str__(self):
        repr_ = f"__Title__: {self.title}"

        if len(self.text) > 100:
            repr_ += f"\n\n {self.text[:50]} [...] {self.text[:-50]}"
        else:
            repr_ += f"\n\n {self.text}"

        return repr_
