""" Implementation of search interface to be used for tests where ElasticSearch is unavailable """
import copy
from datetime import datetime
import json
import os

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder

from search.search_engine_base import SearchEngine
from search.utils import ValueRange, DateRange


def json_date_to_datetime(json_date_string_value):
    ''' converts json date string to date object '''
    if "T" in json_date_string_value:
        if "." in json_date_string_value:
            format_string = "%Y-%m-%dT%H:%M:%S.%f"
        else:
            format_string = "%Y-%m-%dT%H:%M:%S"
        if json_date_string_value.endswith("Z"):
            format_string += "Z"

    else:
        format_string = "%Y-%m-%d"

    return datetime.strptime(
        json_date_string_value,
        format_string
    )


def _find_field(doc, field_name):
    """ find the dictionary field corresponding to the . limited name """
    if not isinstance(doc, dict):
        raise ValueError('Parameter `doc` should be a python dict object')

    if not isinstance(field_name, basestring):
        raise ValueError('Parameter `field_name` should be a string')

    immediate_field, remaining_path = field_name.split('.', 1) if '.' in field_name else (field_name, None)
    field_value = doc.get(immediate_field)

    if isinstance(field_value, dict):
        return _find_field(field_value, remaining_path)
    else:
        return field_value


def _filter_intersection(documents_to_search, dictionary_object, include_blanks=False):
    """
    Filters out documents that do not match all of the field values within the dictionary_object
    If include_blanks is True, then the document is considered a match if the field is not present
    """
    if not dictionary_object:
        return documents_to_search

    def value_matches(doc, field_name, field_value):
        """
        Does the document's field match the desired value, or is the field not present if include_blanks is True
        """
        compare_value = _find_field(doc, field_name)
        if compare_value is None:
            return include_blanks

        # if we have a string that we are trying to process as a date object
        if (isinstance(compare_value, basestring) and
                (isinstance(field_value, DateRange) or isinstance(field_value, datetime))):
            compare_value = json_date_to_datetime(compare_value)

        if isinstance(field_value, ValueRange):
            return (
                (field_value.lower is None or compare_value >= field_value.lower)
                and
                (field_value.upper is None or compare_value <= field_value.upper)
            )
        else:
            return compare_value == field_value

    filtered_documents = documents_to_search
    for field_name, field_value in dictionary_object.items():
        filtered_documents = [d for d in filtered_documents if value_matches(d, field_name, field_value)]

    return filtered_documents


def _process_query_string(documents_to_search, search_strings):
    """ keep the documents that contain at least one of the search strings provided """
    def has_string(dictionary_object, search_string):
        """ search for string in dictionary items, look down into nested dictionaries """
        for name in dictionary_object:
            if isinstance(dictionary_object[name], dict):
                return has_string(dictionary_object[name], search_string)
            elif search_string in dictionary_object[name]:
                return True
        return False

    documents_to_keep = []
    for search_string in search_strings:
        documents_to_keep.extend(
            [d for d in documents_to_search if "content" in d and has_string(d["content"], search_string)]
        )

    return documents_to_keep


class MockSearchEngine(SearchEngine):

    """
    Mock implementation of SearchEngine for test purposes
    """
    _mock_elastic = {}
    _disabled = False
    _file_name_override = None

    @classmethod
    def create_test_file(cls, file_name=None, index_content=None):
        """ creates test file from settings """
        if index_content:
            cls._mock_elastic = index_content
        else:
            cls._mock_elastic = {}
        if file_name:
            cls._file_name_override = file_name
        cls._write_to_file(create_if_missing=True)

    @classmethod
    def destroy_test_file(cls):
        """ creates test file from settings """
        file_name = cls._backing_file()
        if os.path.exists(file_name):
            os.remove(file_name)

        cls._file_name_override = None
        cls.destroy()
        cls.__disabled = False

    @classmethod
    def _backing_file(cls, create_if_missing=False):
        """ return path to test file to use for backing purposes """
        backing_file_name = getattr(settings, "MOCK_SEARCH_BACKING_FILE", None)
        if cls._file_name_override:
            backing_file_name = cls._file_name_override

        if not backing_file_name:
            cls._disabled = False
            return None

        if create_if_missing or os.path.exists(backing_file_name):
            cls._disabled = False
            return backing_file_name

        cls._disabled = True
        return None

    @classmethod
    def _write_to_file(cls, create_if_missing=False):
        """ write the index dict to the backing file """
        file_name = cls._backing_file(create_if_missing)
        if file_name:
            with open(file_name, "w+") as dict_file:
                json.dump(cls._mock_elastic, dict_file, cls=DjangoJSONEncoder)

    @classmethod
    def _load_from_file(cls):
        """ load the index dict from the contents of the backing file """
        file_name = cls._backing_file()
        if file_name and os.path.exists(file_name):
            with open(file_name, "r") as dict_file:
                cls._mock_elastic = json.load(dict_file)

    @staticmethod
    def _paginate_results(size, from_, raw_results):
        """ Give the correct page of results """
        results = raw_results
        if size:
            start = 0
            if from_ is not None:
                start = from_
            results = raw_results[start:start + size]

        return results

    @classmethod
    def load_index(cls, index_name):
        """ load the index, if necessary from the backed file """
        cls._load_from_file()
        if index_name not in cls._mock_elastic:
            cls._mock_elastic[index_name] = {}
            cls._write_to_file()

        return cls._mock_elastic[index_name]

    @classmethod
    def load_doc_type(cls, index_name, doc_type):
        """ load the documents of type doc_type, if necessary loading from the backed file """
        index = cls.load_index(index_name)
        if doc_type not in index:
            index[doc_type] = []
            cls._write_to_file()

        return index[doc_type]

    @classmethod
    def add_document(cls, index_name, doc_type, body):
        """ add document of specific type to index """
        cls.load_doc_type(index_name, doc_type).append(body)
        cls._write_to_file()

    @classmethod
    def remove_document(cls, index_name, doc_type, doc_id):
        """ remove document by id of specific type to index """
        index = cls.load_index(index_name)
        if doc_type not in index:
            return

        index[doc_type] = [d for d in index[doc_type] if "id" not in d or d["id"] != doc_id]
        cls._write_to_file()

    @classmethod
    def destroy(cls):
        """ Clean out the dictionary for test resets """
        cls._mock_elastic = {}
        cls._write_to_file()

    def __init__(self, index=None):
        super(MockSearchEngine, self).__init__(index)
        MockSearchEngine.load_index(self.index_name)

    def index(self, doc_type, body):
        """ Add document of given type to the index """
        if MockSearchEngine._disabled:
            return None
        MockSearchEngine.add_document(self.index_name, doc_type, body)

    def remove(self, doc_type, doc_id):
        """ Remove document of type with given id from the index """
        if MockSearchEngine._disabled:
            return None
        MockSearchEngine.remove_document(self.index_name, doc_type, doc_id)

    def search(self, query_string=None, field_dictionary=None, filter_dictionary=None, **kwargs):
        """ Perform search upon documents within index """
        if MockSearchEngine._disabled:
            return {
                "took": 10,
                "total": 0,
                "max_score": 0,
                "results": []
            }

        documents_to_search = []
        if "doc_type" in kwargs:
            documents_to_search = MockSearchEngine.load_doc_type(self.index_name, kwargs["doc_type"])
        else:
            index = MockSearchEngine.load_index(self.index_name)
            for doc_type in index:
                documents_to_search.extend(index[doc_type])

        if field_dictionary:
            documents_to_search = _filter_intersection(documents_to_search, field_dictionary)

        if filter_dictionary:
            documents_to_search = _filter_intersection(documents_to_search, filter_dictionary, True)

        if query_string:
            documents_to_search = _process_query_string(documents_to_search, query_string.split(" "))

        # Finally, find duplicates and give them a higher score
        search_results = []
        max_score = 0
        while len(documents_to_search) > 0:
            current_doc = documents_to_search[0]
            score = len([d for d in documents_to_search if d == current_doc])
            if score > max_score:
                max_score = score
            documents_to_search = [d for d in documents_to_search if d != current_doc]

            data = copy.copy(current_doc)
            search_results.append(
                {
                    "score": score,
                    "data": data,
                }
            )

        results = MockSearchEngine._paginate_results(
            kwargs["size"] if "size" in kwargs else None,
            kwargs["from_"] if "from_" in kwargs else None,
            sorted(search_results, key=lambda k: k["score"])
        )
        return {
            "took": 10,
            "total": len(search_results),
            "max_score": max_score,
            "results": results
        }
