import re
import csv
import time
import tarfile
import zlib
import logging
import pickle
import multiprocessing as mp
import xml.etree.ElementTree as ET

from io import BytesIO
from ftplib import FTP
from functools import wraps
from argparse import ArgumentParser
from datetime import datetime, timedelta
from os import path, remove, rename, listdir

from indra.literature.crossref_client import get_publisher
from indra.literature.pubmed_client import get_metadata_for_ids
from indra.literature.elsevier_client import download_article_from_ids

from indra.util import zip_string
from indra.literature import pubmed_client
from indra.literature.pmc_client import id_lookup
from indra.util import UnicodeXMLTreeBuilder as UTB

from indra_db.util import get_primary_db, get_db
from indra_db.databases import texttypes, formats
from indra_db.databases import sql_expressions as sql_exp
from indra_db.util.data_gatherer import DataGatherer, DGContext


try:
    from psycopg2 import DatabaseError
except ImportError:
    class DatabaseError(object):
        "Using this in a try-except will catch nothing. (That's the point.)"
        pass

logger = logging.getLogger(__name__)

ftp_blocksize = 33554432  # Chunk size recommended by NCBI
THIS_DIR = path.dirname(path.abspath(__file__))


gatherer = DataGatherer('content', ['refs', 'content'])


class UploadError(Exception):
    pass


class _NihFtpClient(object):
    """High level access to the NIH FTP repositories.

    Parameters
    ----------
    my_path : str
        The path to the subdirectory around which this client operates.
    ftp_url : str
        The url to the ftp site. May be a local directory (see `local`). By
        default this is `'ftp.ncbi.nlm.nih.gov'`.
    local : bool
        These methods may be run on a local directory (intended for testing).
        (default is `False`).
    """
    def __init__(self, my_path, ftp_url='ftp.ncbi.nlm.nih.gov', local=False):
        self.my_path = my_path
        self.is_local = local
        self.ftp_url = ftp_url
        return

    def _path_join(self, *args):
        joined_str = path.join(*args)
        part_list = joined_str.split('/')
        for part in part_list[1:]:
            if part == '..':
                idx = part_list.index(part) - 1
                part_list.pop(idx)
                part_list.pop(idx)
        ret = path.join(*part_list)
        if part_list[0] == '':
            ret = '/' + ret
        return ret

    def get_ftp_connection(self, ftp_path=None):
        if ftp_path is None:
            ftp_path = self.my_path
        # Get an FTP connection
        ftp = FTP(self.ftp_url)
        ftp.login()
        # Change to the manuscripts directory
        ftp.cwd(ftp_path)
        return ftp

    def get_xml_file(self, xml_file):
        "Get the content from an xml file as an ElementTree."
        logger.info("Downloading %s" % (xml_file))
        xml_bytes = self.get_uncompressed_bytes(xml_file, force_str=False)
        logger.info("Parsing XML metadata")
        return ET.XML(xml_bytes, parser=UTB())

    def get_csv_as_dict(self, csv_file, cols=None, header=None):
        "Get the content from a csv file as a list of dicts."
        csv_str = self.get_file(csv_file)
        lst = [row for row in csv.reader(csv_str.splitlines())]
        if cols is None:
            if header is not None:
                cols = lst[header]
                lst = lst[header+1:]
            else:
                cols = list(range(len(lst[0])))
        return [dict(zip(cols, row)) for row in lst]

    def ret_file(self, f_path, buf):
        "Load the content of a file into the given buffer."
        full_path = self._path_join(self.my_path, f_path)
        if not self.is_local:
            with self.get_ftp_connection() as ftp:
                ftp.retrbinary('RETR /%s' % full_path,
                               callback=lambda s: buf.write(s),
                               blocksize=ftp_blocksize)
                buf.flush()
        else:
            with open(self._path_join(self.ftp_url, full_path), 'rb') as f:
                buf.write(f.read())
                buf.flush()
        return

    def download_file(self, f_path, dest=None):
        "Download a file into a file given by f_path."
        name = path.basename(f_path)
        if dest is not None:
            name = path.join(dest, name)
        with open(name, 'wb') as gzf:
            self.ret_file(f_path, gzf)
        return name

    def get_file(self, f_path, force_str=True, decompress=True):
        "Get the contents of a file as a string."
        gzf_bytes = BytesIO()
        self.ret_file(f_path, gzf_bytes)
        ret = gzf_bytes.getvalue()
        if f_path.endswith('.gz') and decompress:
            ret = zlib.decompress(ret, 16+zlib.MAX_WBITS)
        if force_str and isinstance(ret, bytes):
            ret = ret.decode('utf8')
        return ret

    def get_uncompressed_bytes(self, f_path, force_str=True):
        "Get a file that is gzipped, and return the unzipped string."
        return self.get_file(f_path, force_str=force_str, decompress=True)

    def ftp_ls_timestamped(self, ftp_path=None):
        "Get all contents and metadata in mlsd format from the ftp directory."
        if ftp_path is None:
            ftp_path = self.my_path
        else:
            ftp_path = self._path_join(self.my_path, ftp_path)

        if not self.is_local:
            with self.get_ftp_connection(ftp_path) as ftp:
                raw_contents = ftp.mlsd()
                contents = [(k, meta['modify']) for k, meta in raw_contents
                            if not k.startswith('.')]
        else:
            dir_path = self._path_join(self.ftp_url, ftp_path)
            raw_contents = listdir(dir_path)
            contents = [(fname, path.getmtime(path.join(dir_path, fname)))
                        for fname in raw_contents]
        return contents

    def ftp_ls(self, ftp_path=None):
        "Get a list of the contents in the ftp directory."
        if ftp_path is None:
            ftp_path = self.my_path
        else:
            ftp_path = self._path_join(self.my_path, ftp_path)
        if not self.is_local:
            with self.get_ftp_connection(ftp_path) as ftp:
                contents = ftp.nlst()
        else:
            contents = listdir(self._path_join(self.ftp_url, ftp_path))
        return contents


def get_clean_id(db, id_type, id_val):
    if id_type == 'pmid':
        id_val, _ = db.TextRef.process_pmid(id_val)
    elif id_type == 'pmcid':
        id_val, _, _ = db.TextRef.process_pmcid(id_val)
    elif id_type == 'doi':
        id_val, _, _ = db.TextRef.process_doi(id_val)
    return id_val


class ContentManager(object):
    """Abstract class for all upload/update managers.

    This abstract class provides the api required for any object that is
    used to manage content between the database and the content.
    """
    my_source = NotImplemented
    tr_cols = NotImplemented
    err_patt = re.compile('.*?constraint "(.*?)".*?Key \((.*?)\)=\((.*?)\).*?',
                          re.DOTALL)

    def __init__(self):
        self.review_fname = None
        return

    def copy_into_db(self, db, tbl_name, data, cols=None):
        """Wrapper around the db.copy feature, pickles args upon exception.

        This function also regularizes any text ref data put into the database.
        """

        # Handle the breaking of text ref IDs into smaller more searchable bits
        if tbl_name == 'text_ref':
            logger.info("Processing text ref rows.")
            # Create new cols
            if cols is not None and cols != self.tr_cols:
                raise ValueError("Invalid `cols` passed for text_ref.")

            new_cols = []
            for id_type in self.tr_cols:
                if id_type == 'pmid':
                    new_cols += ['pmid', 'pmid_num']
                elif id_type == 'pmcid':
                    new_cols += ['pmcid', 'pmcid_num', 'pmcid_version']
                elif id_type == 'doi':
                    new_cols += ['doi', 'doi_ns', 'doi_id']
                else:
                    new_cols.append(id_type)
            cols = tuple(new_cols)

            # Process all the rows.
            new_data = []
            for row in data:
                if len(row) != len(self.tr_cols):
                    raise ValueError("Row length does not match column length "
                                     "of labels.")
                new_row = []
                for id_type, id_val in zip(self.tr_cols, row):
                    if id_type == 'pmid':
                        pmid, pmid_num = db.TextRef.process_pmid(id_val)
                        new_row += [pmid, pmid_num]
                    elif id_type == 'pmcid':
                        pmcid, pmcid_num, pmcid_version = \
                            db.TextRef.process_pmcid(id_val)
                        new_row += [pmcid, pmcid_num, pmcid_version]
                    elif id_type == 'doi':
                        doi, doi_ns, doi_id = db.TextRef.process_doi(id_val)
                        new_row += [doi, doi_ns, doi_id]
                    else:
                        new_row.append(id_val)
                new_data.append(tuple(new_row))
            data = new_data

        return db.copy_report_lazy(tbl_name, data, cols)

    def make_text_ref_str(self, tr):
        """Make a string from a text ref using tr_cols."""
        return str([getattr(tr, id_type) for id_type in self.tr_cols])

    def add_to_review(self, desc, msg):
        """Add an entry to the review document."""
        # NOTE: If this is ever done on AWS or through a
        # container, the review file MUST be loaded somewhere
        # it won't disappear. (such as s3). Perhaps these could
        # be logged on the database?
        logger.warning("Found \"%s\"! Check %s."
                       % (desc, self.review_fname))
        with open(self.review_fname, 'a+') as f:
            f.write(msg + '\n')
        return

    def filter_text_refs(self, db, tr_data_set, primary_id_types=None):
        """Try to reconcile the data we have with what's already on the db.

        Note that this method is VERY slow in general, and therefore should
        be avoided whenever possible.

        The process can be sped up considerably by multiple orders of
        magnitude if you specify a limited set of id types to query to get
        text refs. This does leave some possibility of missing relevant refs.
        """
        logger.info("Beginning to filter %d text refs..." % len(tr_data_set))

        # This is a helper for accessing the data tuples we create
        def id_idx(id_type):
            return self.tr_cols.index(id_type)

        # If there are not actual refs to work with, don't waste time.
        N = len(tr_data_set)
        if not N:
            return set(), []

        # Get all text refs that match any of the id data we have.
        logger.debug("Getting list of existing text refs...")
        or_list = []
        if primary_id_types is not None:
            match_id_types = primary_id_types
        else:
            match_id_types = self.tr_cols
        # Get IDs from the tr_data_set that have one or more of the listed
        # id types.
        for id_type in match_id_types:
            id_list = [entry[id_idx(id_type)] for entry in tr_data_set
                       if entry[id_idx(id_type)] is not None]
            # Add SqlAlchemy filter clause based on ID list for this ID type
            if id_list:
                if id_type == 'pmid':
                    term = db.TextRef.pmid_in(id_list, filter_ids=True)
                    bad_ids = [pmid for pmid in id_list
                               if not all(db.TextRef.process_pmid(pmid))]
                elif id_type == 'pmcid':
                    term = db.TextRef.pmcid_in(id_list, filter_ids=True)
                    bad_ids = [pmcid for pmcid in id_list
                               if not all(db.TextRef.process_pmcid(pmcid)[:2])]
                elif id_type == 'doi':
                    term = db.TextRef.doi_in(id_list, filter_ids=True)
                    bad_ids = [doi for doi in id_list
                               if not all(db.TextRef.process_doi(doi))]
                else:
                    term = getattr(db.TextRef, id_type).in_(id_list)
                    bad_ids = []
                or_list.append(term)
                if bad_ids:
                    logger.info("Handling %d malformed '%s's with separate "
                                "query." % (len(bad_ids), id_type))
                    or_list.append(getattr(db.TextRef, id_type).in_(bad_ids))
        if len(or_list) == 1:
            tr_list = db.select_all(db.TextRef, or_list[0])
        else:
            tr_list = db.select_all(db.TextRef, sql_exp.or_(*or_list))
        logger.debug("Found %d potentially relevant text refs." % len(tr_list))

        # Create an index of tupled data entries for quick lookups by any id
        # type, for example tr_data_idx_dict['pmid'][<a pmid>] will get the
        # tuple with all the id data.
        logger.debug("Building index of new data...")
        tr_data_idx_dict = {}
        for id_type in self.tr_cols:
            tr_data_idx = {}
            for entry in tr_data_set:
                id_val = entry[id_idx(id_type)]
                try:
                    id_val = get_clean_id(db, id_type, id_val)
                except Exception as err:
                    logger.warning(f"Id of type {id_type} malformed: {id_val}")

                if id_val is not None:
                    tr_data_idx[id_val] = entry
            tr_data_idx_dict[id_type] = tr_data_idx
            del tr_data_idx

        # Look for updates to the existing text refs
        logger.debug("Beginning to iterate over text refs...")
        tr_data_match_list = []
        flawed_tr_data = []
        multi_match_records = set()
        update_dict = {}

        def add_to_found_record_list(record):
            # Adds a record tuple from tr_data_list to tr_data_match_list and
            # return True on success. If the record has multiple matches in
            # the database then we wouldn't know which one to update--hence
            # we record for review and return False for failure.
            if record not in tr_data_match_list:
                tr_data_match_list.append(record)
                added = True
            else:
                self.add_to_review(
                    "tr matching input record matched to another tr",
                    "Input record %s already matched. Matched again to %s."
                    % (record, self.make_text_ref_str(tr))
                    )
                flawed_tr_data.append(('over_match_db', record))
                multi_match_records.add(record)
                added = False
            return added

        for tr in tr_list:
            match_set = set()

            # Find the matches in the data. Multiple distinct matches indicate
            # problems, and are flagged.
            for id_type, tr_data_idx in tr_data_idx_dict.items():
                candidate = tr_data_idx.get(getattr(tr, id_type))
                if candidate is not None:
                    match_set.add(candidate)

            # Every tr MUST have a match, or else something is broken.
            assert match_set, "No matches found, which is impossible."

            # Given a unique match, update any missing ids from the input data.
            if len(match_set) == 1:
                tr_new = match_set.pop()

                # Add this record to the match list, unless there are conflicts
                # If there are conflicts (multiple matches in the DB) then
                # we skip any updates.
                if not add_to_found_record_list(tr_new):
                    continue

                # Tabulate new/updated ID information.
                # Go through all the id_types
                all_good = True
                id_updates = {}
                for i, id_type in enumerate(self.tr_cols):
                    # Check if the text ref is missing that id.
                    if getattr(tr, id_type) is None:
                        # If so, and if our new data does have that id, update
                        # the text ref.
                        if tr_new[i] is not None:
                            logger.debug("Will update text ref for %s: %s."
                                         % (id_type, tr_new[i]))
                            id_updates[id_type] = tr_new[i]
                    else:
                        # Check to see that all the ids agree. If not, report
                        # it in the review.txt file.
                        new_id = get_clean_id(db, id_type, tr_new[i])
                        if new_id is None:
                            continue
                        elif new_id != getattr(tr, id_type):
                            self.add_to_review(
                                'conflicting ids',
                                'Got conflicting %s: in db %s vs %s.'
                                % (id_type, self.make_text_ref_str(tr), tr_new)
                                )
                            flawed_tr_data.append((id_type, tr_new))
                            all_good = False

                if all_good and len(id_updates):
                    update_dict[tr.id] = (tr, id_updates, tr_new)
            else:
                # These still matched something in the db, so they shouldn't be
                # uploaded as new refs.
                for tr_new in match_set:
                    add_to_found_record_list(tr_new)
                    flawed_tr_data.append(('over_match_input', tr_new))

                # This condition only occurs if the records we got are
                # internally inconsistent. This is rare, but it can happen.
                self.add_to_review(
                    "multiple matches in records for tex ref",
                    'Multiple matches for %s from %s: %s.'
                    % (self.make_text_ref_str(tr), self.my_source, match_set))

        # Apply ID updates to TextRefs with unique matches in tr_data_set
        logger.info("Applying %d updates." % len(update_dict))
        for tr, id_updates, record in update_dict.values():
            if record not in multi_match_records:
                tr.update(**id_updates)
            else:
                logger.warning("Skipping update of text ref %d with %s due "
                               "to multiple matches to record %s."
                               % (tr.id, id_updates, record))

        # This applies all the changes made to the text refs to the db.
        logger.debug("Committing changes...")
        db.commit("Failed to update with new ids.")

        # Now update the text refs with any new refs that were found
        filtered_tr_records = tr_data_set - set(tr_data_match_list) \
            - multi_match_records

        logger.debug("Filtering complete! %d records remaining."
                     % len(filtered_tr_records))
        return filtered_tr_records, flawed_tr_data

    @classmethod
    def _record_for_review(cls, func):
        @wraps(func)
        def take_action(self, db, *args, **kwargs):
            review_fmt = "review_%s_%s_%%s.txt" % (func.__name__,
                                                   self.my_source)
            self.review_fname = path.join(THIS_DIR, review_fmt % 'in_progress')
            logger.info("Creating review file %s." % self.review_fname)
            open(self.review_fname, 'a+').close()
            completed = func(self, db, *args, **kwargs)
            if completed:
                utcnow = datetime.utcnow()
                is_init_upload = (func.__name__ == 'populate')
                with open(self.review_fname, 'r') as f:
                    conflicts_bytes = zip_string(f.read())
                    db.insert('updates', init_upload=is_init_upload,
                              source=self.my_source,
                              unresolved_conflicts_file=conflicts_bytes)
                rename(self.review_fname,
                       review_fmt % utcnow.strftime('%Y%m%d-%H%M%S'))
            return completed
        return take_action

    def _get_latest_update(self, db):
        """Get the date of the latest update."""
        update_list = db.select_all(db.Updates,
                                    db.Updates.source == self.my_source)
        if not len(update_list):
            logger.error("The database has not had an initial upload, or else "
                         "the updates table has not been populated.")
            return False

        return max([u.datetime for u in update_list])

    def populate(self, db):
        "A stub for the method used to initially populate the database."
        raise NotImplementedError(
            "`Populate` not implemented for `%s`." % self.__class__.__name__
            )

    def update(self, db):
        "A stub for the method used to update the content on the database."
        raise NotImplementedError(
            "`Update` not implemented for `%s`." % self.__class__.__name__
            )


class _NihManager(ContentManager):
    """Abstract class for all the managers that use the NIH FTP service.

    See `_NihFtpClient` for parameters.
    """
    my_path = NotImplemented

    def __init__(self, *args, **kwargs):
        self.ftp = _NihFtpClient(self.my_path, *args, **kwargs)
        super(_NihManager, self).__init__()
        return


class Pubmed(_NihManager):
    "Manager for the pubmed/medline content."
    my_path = 'pubmed'
    my_source = 'pubmed'
    tr_cols = ('pmid', 'pmcid', 'doi', 'pii',)

    def __init__(self, *args, categories=None, tables=None,
                 max_annotations=500000, **kwargs):
        super(Pubmed, self).__init__(*args, **kwargs)
        self.deleted_pmids = None
        if categories is None:
            self.categories = [texttypes.TITLE, texttypes.ABSTRACT]
        else:
            self.categories = categories[:]
        assert all(cat in texttypes.values() for cat in self.categories)

        if tables is None:
            self.tables = ['text_ref', 'text_content']
        else:
            self.tables = tables[:]

        self.db_pmids = None
        self.max_annotations = max_annotations
        self.annotations = {}
        return

    def get_deleted_pmids(self):
        if self.deleted_pmids is None:
            del_pmid_str = self.ftp.get_uncompressed_bytes(
                'deleted.pmids.gz'
                )
            pmid_list = [
                line.strip() for line in del_pmid_str.split('\n')
                ]
            self.deleted_pmids = pmid_list
        return self.deleted_pmids[:]

    def get_file_list(self, sub_dir):
        all_files = self.ftp.ftp_ls(sub_dir)
        return [sub_dir + '/' + k for k in all_files if k.endswith('.xml.gz')]

    def get_article_info(self, xml_file, q=None):
        tree = self.ftp.get_xml_file(xml_file)
        article_info = pubmed_client.get_metadata_from_xml_tree(
            tree,
            get_abstracts=True,
            prepend_title=False
            )
        if q is not None:
            q.put((xml_file, article_info))
            return
        else:
            return article_info

    def fix_doi(self, doi):
        "Sometimes the doi is doubled (no idea why). Fix it."
        if doi is None:
            return
        L = len(doi)
        if L % 2 is not 0:
            return doi
        if doi[:L//2] != doi[L//2:]:
            return doi
        logger.info("Fixing doubled doi: %s" % doi)
        return doi[:L//2]

    def add_annotations(self, db, article_info):
        "Load annotations into the database."
        for pmid, info_dict in article_info.items():
            self.annotations[pmid] = info_dict['mesh_annotations']

        # Add mesh annotations to the db in batches.
        if len(self.annotations) > self.max_annotations:
            self.dump_annotations(db)
            self.annotations = {}

        return

    def load_text_refs(self, db, article_info, carefully=False):
        "Sanitize, update old, and upload new text refs."

        # Remove PMID's listed as deleted.
        deleted_pmids = self.get_deleted_pmids()
        valid_pmids = set(article_info.keys()) - set(deleted_pmids)
        logger.info("%d valid PMIDs" % len(valid_pmids))

        # Remove existing pmids if we're not being careful (this suffices for
        # filtering in the initial upload).
        if not carefully:
            existing_pmids = set(db.get_values(db.select_all(
                db.TextRef,
                db.TextRef.pmid.in_(valid_pmids)
                ), 'pmid'))
            logger.info(
                "%d valid PMIDs already in text_refs." % len(existing_pmids)
                )
            valid_pmids -= existing_pmids
            logger.info("%d PMIDs to add to text_refs" % len(valid_pmids))

        # Convert the article_info into a list of tuples for insertion into
        # the text_ref table

        def get_val(data, id_type):
            r = data.get(id_type)
            if id_type == 'doi':
                r = self.fix_doi(r)
            return None if not r else r.strip().upper()

        text_ref_records = {tuple([pmid]+[get_val(article_info[pmid], id_type)
                                          for id_type in self.tr_cols[1:]])
                            for pmid in valid_pmids}

        # Check the ids more carefully against what is already in the db.
        if carefully:
            text_ref_records, flawed_refs = \
                self.filter_text_refs(db, text_ref_records,
                                      primary_id_types=['pmid', 'pmcid'])
            logger.info('%d new records to add to text_refs.'
                        % len(text_ref_records))
            valid_pmids -= {ref[self.tr_cols.index('pmid')]
                            for cause, ref in flawed_refs
                            if cause in ['pmid', 'over_match']}
            logger.info('Only %d valid for potential content upload.'
                        % len(valid_pmids))

        # Remove the pmids from any data entries that failed to copy.
        vile_data = self.copy_into_db(db, 'text_ref', text_ref_records,
                                      self.tr_cols)
        gatherer.add('refs', len(text_ref_records) - len(vile_data))
        if not vile_data:
            valid_pmids -= {d[self.tr_cols.index('pmid')] for d in vile_data}
        return valid_pmids

    def load_text_content(self, db, article_info, valid_pmids,
                          carefully=False):

        # Build a dict mapping PMIDs to text_ref IDs
        tr_qry = db.filter_query(db.TextRef, db.TextRef.pmid.in_(valid_pmids))
        tref_list = tr_qry.all()
        if not carefully:
            # This doesn't check if there are any existing refs.
            logger.info('There are %d content entries that will be uploaded.'
                        % len(tref_list))
            cat_valid_pmids = {cat: valid_pmids.copy()
                               for cat in self.categories}
        else:
            cat_valid_pmids = {}
            for cat in self.categories:
                # This does...
                tr_to_avoid_qry = tr_qry.filter(
                    db.TextRef.id == db.TextContent.text_ref_id,
                    db.TextContent.source == self.my_source,
                    db.TextContent.text_type == cat
                    )
                cat_valid_pmids[cat] = \
                    valid_pmids - {tr.pmid for tr in tr_to_avoid_qry.all()}
                logger.info("Only %d entries without pre-existing content for "
                            "%s." % (len(cat_valid_pmids[cat]), cat))
        pmid_tr_dict = {pmid: trid for (pmid, trid) in
                        db.get_values(tref_list, ['pmid', 'id'])}

        # Add the text_ref IDs to the content to be inserted
        text_content_records = []
        for cat in self.categories:
            for pmid in cat_valid_pmids[cat]:
                if pmid not in pmid_tr_dict.keys():
                    logger.warning("Found content marked to be uploaded which "
                                   "does not have a text ref. Skipping pmid "
                                   "%s..." % pmid)
                    continue
                tr_id = pmid_tr_dict[pmid]

                content = article_info[pmid].get(cat)
                if content and content.strip():
                    content_gz = zip_string(content)
                    text_content_records.append((tr_id, self.my_source,
                                                 formats.TEXT, cat,
                                                 content_gz))
        logger.info("Found %d new text content entries."
                    % len(text_content_records))

        self.copy_into_db(
            db,
            'text_content',
            text_content_records,
            cols=('text_ref_id', 'source', 'format', 'text_type',
                  'content')
            )
        gatherer.add('content', len(text_content_records))
        return

    def upload_article(self, db, article_info, carefully=False):
        "Process the content of an xml dataset and load into the database."
        logger.info("%d PMIDs in XML dataset" % len(article_info))

        self.add_annotations(db, article_info)

        # Process and load the text refs, updating where appropriate.
        if 'text_ref' in self.tables:
            valid_pmids = self.load_text_refs(db, article_info, carefully)
        else:
            valid_pmids = set(article_info.keys()) & self.db_pmids
            logger.info("%d pmids are valid." % len(valid_pmids))

        if 'text_content' in self.tables:
            self.load_text_content(db, article_info, valid_pmids, carefully)
        return True

    def load_files(self, db, dirname, n_procs=1, continuing=False,
                   carefully=False, log_update=True):
        """Load the files in subdirectory indicated by `dirname`."""
        if 'text_ref' not in self.tables:
            logger.info("Loading pmids from the database...")
            self.db_pmids = {pmid for pmid, in db.select_all(db.TextRef.pmid)}

        xml_files = set(self.get_file_list(dirname))
        if continuing or log_update:
            sf_list = db.select_all(
                db.SourceFile,
                db.SourceFile.source == self.my_source
                )
            existing_files = {sf.name for sf in sf_list if dirname in sf.name}

            if continuing and xml_files == existing_files:
                logger.info("All files have been loaded. Nothing to do.")
                return False
        else:
            existing_files = set()

        logger.info('Beginning upload with %d processes...' % n_procs)
        if n_procs > 1:
            # Download the XML files in parallel
            q = mp.Queue()
            proc_list = []
            for xml_file in sorted(xml_files):
                if continuing and xml_file in existing_files:
                    logger.info("Skipping %s. Already uploaded." % xml_file)
                    continue
                p = mp.Process(
                    target=self.get_article_info,
                    args=(xml_file, q)
                    )
                proc_list.append(p)
            n_tot = len(proc_list)

            for _ in range(n_procs):
                if len(proc_list):
                    p = proc_list.pop(0)
                    p.start()

            def upload_and_record_next(start_new):
                # Wait until at least one article is done.
                xml_file, article_info = q.get()
                if start_new:
                    proc_list.pop(0).start()
                logger.info("Beginning to upload %s." % xml_file)
                self.upload_article(db, article_info, carefully)
                logger.info("Completed %s." % xml_file)
                if log_update and xml_file not in existing_files:
                    db.insert('source_file', source=self.my_source,
                              name=xml_file)

            while len(proc_list):
                upload_and_record_next(True)
                n_tot -= 1

            while n_tot is not 0:
                upload_and_record_next(False)
                n_tot -= 1
        else:
            for xml_file in sorted(xml_files):
                if continuing and xml_file in existing_files:
                    logger.info("Skipping %s. Already uploaded." % xml_file)
                    continue
                article_info = self.get_article_info(xml_file)
                logger.info("Beginning to upload %s." % xml_file)
                self.upload_article(db, article_info, carefully)
                logger.info("Completed %s." % xml_file)
                if log_update and xml_file not in existing_files:
                    db.insert('source_file', source=self.my_source,
                              name=xml_file)

        return True

    def dump_annotations(self, db):
        """Dump all the annotations that have been saved so far."""
        logger.info("Dumping mesh annotations for %d refs."
                    % len(self.annotations))

        # If there are no annotations, don't waste time.
        if not self.annotations:
            return False

        copy_rows = []
        for pmid, annotation_list in self.annotations.items():
            for annotation in annotation_list:
                # Format the row.
                copy_row = (int(pmid), int(annotation['mesh'][1:]),
                            annotation['major_topic'])

                # Handle the qualifier
                qual = annotation['qualifier']
                if qual is None:
                    copy_row += (None,)
                else:
                    copy_row += (int(qual['mesh'][1:]),)

                copy_rows.append(copy_row)

        # Copy the results into the database
        self.copy_into_db(db, 'mesh_ref_annotations', copy_rows,
                          ('pmid_num', 'mesh_num', 'major_topic', 'qual_num'))
        return True

    def load_files_and_annotations(self, db, *args, **kwargs):
        """Thin wrapper around load_files that also loads annotations."""
        try:
            ret = self.load_files(db, *args, **kwargs)
        finally:
            self.dump_annotations(db)
        return ret

    @ContentManager._record_for_review
    @DGContext.wrap(gatherer, 'pubmed')
    def populate(self, db, n_procs=1, continuing=False):
        """Perform the initial input of the pubmed content into the database.

        Parameters
        ----------
        db : indra.db.DatabaseManager instance
            The database to which the data will be uploaded.
        n_procs : int
            The number of processes to use when parsing xmls.
        continuing : bool
            If true, assume that we are picking up after an error, or otherwise
            continuing from an earlier process. This means we will skip over
            source files contained in the database. If false, all files will be
            read and parsed.
        """
        return self.load_files_and_annotations(db, 'baseline', n_procs,
                                               continuing, False)

    @ContentManager._record_for_review
    @DGContext.wrap(gatherer, 'pubmed')
    def update(self, db, n_procs=1):
        """Update the contents of the database with the latest articles."""
        did_base = self.load_files_and_annotations(db, 'baseline', n_procs,
                                                   True, True)
        did_update = self.load_files_and_annotations(db, 'updatefiles',
                                                     n_procs, True, True)
        return did_base or did_update


class PmcManager(_NihManager):
    """Abstract class for uploaders of PMC content: PmcOA and Manuscripts."""
    my_source = NotImplemented
    tr_cols = ('pmid', 'pmcid', 'doi', 'manuscript_id',)

    def __init__(self, *args, **kwargs):
        super(PmcManager, self).__init__(*args, **kwargs)
        self.tc_cols = ('text_ref_id', 'source', 'format', 'text_type',
                        'content',)

    def get_missing_pmids(self, db, tr_data):
        "Try to get missing pmids using the pmc client."

        logger.info("Getting missing pmids.")

        missing_pmid_entries = []
        for tr_entry in tr_data:
            if tr_entry['pmid'] is None:
                missing_pmid_entries.append(tr_entry)

        num_missing = len(missing_pmid_entries)
        if num_missing is 0:
            logger.debug("No missing pmids.")
            return

        logger.debug('Missing %d pmids.' % num_missing)
        tr_list = db.select_all(
            db.TextRef, db.TextRef.pmcid.in_(
                [tr_entry['pmcid'] for tr_entry in missing_pmid_entries]
                )
            )
        pmids_from_db = {tr.pmcid: tr.pmid for tr in tr_list
                         if tr.pmid is not None}

        logger.debug("Found %d pmids on the databse." % len(pmids_from_db))
        num_found_non_db = 0
        for tr_entry in missing_pmid_entries:
            if tr_entry['pmcid'] not in pmids_from_db.keys():
                ret = id_lookup(tr_entry['pmcid'], idtype='pmcid')
                if 'pmid' in ret.keys() and ret['pmid'] is not None:
                    tr_entry['pmid'] = ret['pmid']
                    num_found_non_db += 1
                    num_missing -= 1
            else:
                tr_entry['pmid'] = pmids_from_db[tr_entry['pmcid']]
                num_missing -= 1
        logger.debug("Found %d more pmids from other sources."
                     % num_found_non_db)
        logger.debug("There are %d missing pmids remaining." % num_missing)
        return

    def filter_text_content(self, db, tc_data):
        'Filter the text content to identify pre-existing records.'
        logger.info("Beginning to filter text content...")
        arc_pmcid_list = [tc['pmcid'] for tc in tc_data]
        if not len(tc_data):
            return []

        logger.info("Getting text refs for pmcid->trid dict..")
        tc_q = (db.session.query(db.TextContent.text_ref_id,
                                 db.TextContent.text_type)
                .filter(db.TextContent.source == self.my_source,
                        db.TextContent.format == formats.XML))
        tc_al = tc_q.subquery().alias('tc')
        q = (db.session.query(db.TextRef.pmcid, db.TextRef.id,
                              tc_al.c.text_type)
             .outerjoin(tc_al)
             .filter(db.TextRef.pmcid_in(arc_pmcid_list)))
        existing_tc_meta = q.all()

        pmcid_trid_dict = {pmcid: trid for pmcid, trid, _ in existing_tc_meta}

        # This should be a very small list, in general.
        existing_tc_records = [
            (trid, self.my_source, formats.XML, text_type)
            for _, trid, text_type in existing_tc_meta if text_type is not None
            ]
        logger.info("Found %d existing records on the db."
                    % len(existing_tc_records))
        tc_records = []
        for tc in tc_data:
            if tc['pmcid'] not in pmcid_trid_dict.keys():
                logger.warning("Found pmcid (%s) among text content data, but "
                               "not in the database. Skipping." % tc['pmcid'])
                continue
            tc_records.append(
                (
                    pmcid_trid_dict[tc['pmcid']],
                    self.my_source,
                    formats.XML,
                    tc['text_type'],
                    tc['content']
                    )
                )
        filtered_tc_records = [
            rec for rec in tc_records if rec[:-1] not in existing_tc_records
            ]
        logger.info("Finished filtering the text content.")
        return list(set(filtered_tc_records))

    def upload_batch(self, db, tr_data, tc_data):
        "Add a batch of text refs and text content to the database."

        # Check for any pmids we can get from the pmc client (this is slow!)
        self.get_missing_pmids(db, tr_data)

        # Turn the list of dicts into a set of tuples
        tr_data_set = {tuple([entry[id_type] for id_type in self.tr_cols])
                       for entry in tr_data}

        filtered_tr_records, flawed_tr_records = \
            self.filter_text_refs(db, tr_data_set,
                                  primary_id_types=['pmid', 'pmcid',
                                                    'manuscript_id'])
        pmcids_to_skip = {rec[self.tr_cols.index('pmcid')]
                          for cause, rec in flawed_tr_records
                          if cause in ['pmcid', 'over_match_input',
                                       'over_match_db']}
        if len(pmcids_to_skip) is not 0:
            mod_tc_data = [
                tc for tc in tc_data if tc['pmcid'] not in pmcids_to_skip
                ]
        else:
            mod_tc_data = tc_data

        # Upload the text content data.
        logger.info('Adding %d new text refs...' % len(filtered_tr_records))
        self.copy_into_db(
            db,
            'text_ref',
            filtered_tr_records,
            self.tr_cols
            )
        gatherer.add('refs', len(filtered_tr_records))

        # Process the text content data
        filtered_tc_records = self.filter_text_content(db, mod_tc_data)

        # Upload the text content data.
        logger.info('Adding %d more text content entries...' %
                    len(filtered_tc_records))
        self.copy_into_db(
            db,
            'text_content',
            filtered_tc_records,
            self.tc_cols
            )
        gatherer.add('content', len(filtered_tc_records))
        return

    def get_data_from_xml_str(self, xml_str, filename):
        "Get the data out of the xml string."
        try:
            tree = ET.XML(xml_str.encode('utf8'))
        except ET.ParseError:
            logger.info("Could not parse %s. Skipping." % filename)
            return None
        id_data = {
            e.get('pub-id-type'): e.text for e in
            tree.findall('.//article-id')
            }
        if 'pmc' not in id_data.keys():
            logger.info("Did not get a 'pmc' in %s." % filename)
            return None
        if 'pmcid' not in id_data.keys():
            id_data['pmcid'] = 'PMC' + id_data['pmc']
        if 'manuscript' in id_data.keys():
            id_data['manuscript_id'] = id_data['manuscript']
        tr_datum_raw = {k: id_data.get(k) for k in self.tr_cols}
        tr_datum = {k: val.strip().upper() if val is not None else None
                    for k, val in tr_datum_raw.items()}
        tc_datum = {
            'pmcid': id_data['pmcid'],
            'text_type': texttypes.FULLTEXT,
            'content': zip_string(xml_str)
            }
        return tr_datum, tc_datum

    def unpack_archive_path(self, archive_path, q=None, db=None,
                            batch_size=10000):
        """"Unpack the contents of an archive.

        If `q` is given, then the data is put into the que to be handed off for
        upload by another process. Otherwise, if `db` is provided, upload the
        batches of data on this process. One or the other MUST be provided.
        """
        with tarfile.open(archive_path, mode='r:gz') as tar:
            xml_files = [m for m in tar.getmembers() if m.isfile()
                         and m.name.endswith('xml')]
            N_tot = len(xml_files)
            logger.info('Loading %s which contains %d files.'
                        % (path.basename(archive_path), N_tot))
            N_batches = N_tot//batch_size + 1
            for i in range(N_batches):
                tr_data = []
                tc_data = []
                for xml_file in xml_files[i*batch_size:(i+1)*batch_size]:
                    xml_str = tar.extractfile(xml_file).read().decode('utf8')
                    res = self.get_data_from_xml_str(xml_str, xml_file.name)
                    if res is None:
                        continue
                    else:
                        tr, tc = res
                    tr_data.append(tr)
                    tc_data.append(tc)

                if q is not None:
                    label = (i+1, N_batches, path.basename(archive_path))
                    logger.debug("Submitting batch %d/%d for %s to queue."
                                 % label)
                    q.put((label, tr_data[:], tc_data[:]))
                elif db is not None:
                    self.upload_batch(db, tr_data[:], tc_data[:])
                else:
                    raise UploadError(
                        "unpack_archive_path must receive either a db instance"
                        " or a queue instance."
                        )
        return

    def process_archive(self, archive, q=None, db=None, continuing=False):
        """Download an archive and begin unpacking it.

        Either `q` or `db` must be specified. The uncompressed contents of the
        archive will be loaded onto the database or placed on the queue in
        batches.

        Parameters
        ----------
        archive : str
            The path of the archive beneath the head of this sources ftp
            directory.
        q : multiprocessing.Queue
            When this method is called as a separate process, the contents of
            the archive are posted to a queue in batches to be handled
            externally.
        db : indra.db.DatabaseManager
            When not multprocessing, the contents of the archive are uploaded
            to the database directly by this method.
        continuing : bool
            True if this method is being called to complete an earlier failed
            attempt to execute this method; will not download the archive if an
            archive of the same name is already downloaded locally. Default is
            False.
        """

        # This is a guess at the location of the archive.
        archive_local_path = path.join(THIS_DIR, path.basename(archive))

        # Download the archive if need be.
        if continuing and path.exists(archive_local_path):
            logger.info('Archive %s found locally at %s, not loading again.'
                        % (archive, archive_local_path))
        else:
            logger.info('Downloading archive %s.' % archive)
            try:
                archive_local_path = self.ftp.download_file(archive,
                                                            dest=THIS_DIR)
                logger.debug("Download succesfully completed for %s."
                             % archive)
            except BaseException:
                logger.error("Failed to download %s. Deleting corrupt file."
                             % archive)
                remove(archive_local_path)
                raise

        # Now unpack the archive.
        self.unpack_archive_path(archive_local_path, q=q, db=db)

        # Assuming we completed correctly, remove the archive.
        logger.info("Removing %s." % archive_local_path)
        remove(archive_local_path)
        return

    def is_archive(self, *args):
        raise NotImplementedError("is_archive must be defined by the child.")

    def get_file_list(self):
        return [k for k in self.ftp.ftp_ls() if self.is_archive(k)]

    def upload_archives(self, db, archives, n_procs=1, continuing=False):
        "Do the grunt work of downloading and processing a list of archives."
        q = mp.Queue(len(archives))
        wait_list = []
        for archive in archives:
            p = mp.Process(
                target=self.process_archive,
                args=(archive, ),
                kwargs={'q': q, 'continuing': continuing},
                daemon=True
                )
            wait_list.append((archive, p))

        active_list = []

        def start_next_proc():
            if len(wait_list) is not 0:
                archive, proc = wait_list.pop(0)
                proc.start()
                active_list.append((archive, proc))

        # Start the processes running
        for _ in range(min(n_procs, len(archives))):
            start_next_proc()

        # Monitor the processes while any are still active.
        batch_log = path.join(THIS_DIR, '%s_batch_log.tmp' % self.my_source)
        batch_entry_fmt = '%s %d\n'
        open(batch_log, 'a+').close()
        while len(active_list) is not 0:
            # Check for processes that have been unpacking archives to
            # complete, and when they do, add them to the source_file table.
            # If there are any more archives waiting to be processed, start
            # the next one.
            for a, p in [(a, p) for a, p in active_list if not p.is_alive()]:
                if p.exitcode is 0:
                    sf_list = db.select_all(
                        db.SourceFile,
                        db.SourceFile.source == self.my_source,
                        db.SourceFile.name == a
                        )
                    if not sf_list:
                        db.insert('source_file', source=self.my_source, name=a)
                else:
                    logger.error("Process for %s exitted with exit code %d."
                                 % (path.basename(a), p.exitcode))
                active_list.remove((a, p))
                start_next_proc()

            # Wait for the next output from an archive unpacker.
            try:
                # This will not block until at least one is done
                label, tr_data, tc_data = q.get_nowait()
            except Exception:
                continue
            logger.info("Beginning to upload batch %d/%d from %s..." % label)
            batch_id, _, arc_name = label
            if continuing:
                with open(batch_log, 'r') as f:
                    if batch_entry_fmt % (arc_name, batch_id) in f.readlines():
                        logger.info("Batch %d already completed: skipping..."
                                    % batch_id)
                        continue
            self.upload_batch(db, tr_data, tc_data)
            with open(batch_log, 'a+') as f:
                f.write(batch_entry_fmt % (arc_name, batch_id))
            logger.info("Finished batch %d/%d from %s..." % label)
            time.sleep(0.1)

        # Empty the queue.
        while not q.empty():
            try:
                tr_data, tc_data = q.get(timeout=1)
            except Exception:
                break
            self.upload_batch(db, tr_data, tc_data)

        remove(batch_log)

        return

    @ContentManager._record_for_review
    @DGContext.wrap(gatherer)
    def populate(self, db, n_procs=1, continuing=False):
        """Perform the initial population of the pmc content into the database.

        Parameters
        ----------
        db : indra.db.DatabaseManager instance
            The database to which the data will be uploaded.
        n_procs : int
            The number of processes to use when parsing xmls.
        continuing : bool
            If true, assume that we are picking up after an error, or
            otherwise continuing from an earlier process. This means we will
            skip over source files contained in the database. If false, all
            files will be read and parsed.

        Returns
        -------
        completed : bool
            If True, an update was completed. Othewise, the updload was aborted
            for some reason, often because the upload was already completed
            at some earlier time.
        """
        gatherer.set_sub_label(self.my_source)
        archives = set(self.get_file_list())

        if continuing:
            sf_list = db.select_all(
                'source_file',
                db.SourceFile.source == self.my_source
                )
            for sf in sf_list:
                logger.info("Skipping %s, already done." % sf.name)
                archives.remove(sf.name)

            # Don't do unnecessary work.
            if not len(archives):
                logger.info("No archives to load. All done.")
                return False

        self.upload_archives(db, archives, n_procs, continuing=continuing)
        return True


class PmcOA(PmcManager):
    """ContentManager for the pmc open access content.

    For further details on the API, see the parent class: PmcManager.
    """
    my_path = 'pub/pmc'
    my_source = 'pmc_oa'

    def is_archive(self, k):
        return k.startswith('articles') and k.endswith('.xml.tar.gz')

    @ContentManager._record_for_review
    @DGContext.wrap(gatherer, 'pmc_oa')
    def update(self, db, n_procs=1):
        min_datetime = self._get_latest_update(db)

        # Search down through the oa_package directory. Below the first level,
        # the files are timestamped, so we can filter down each level
        # efficiently finding the latest files to update.
        logger.info("Getting list of articles that have been uploaded since "
                    "the last update.")
        files = self.ftp.get_csv_as_dict('oa_file_list.csv', header=0)
        fpath_set = {
            f['File'] for f in files
            if datetime.strptime(f['Last Updated (YYYY-MM-DD HH:MM:SS)'],
                                 '%Y-%m-%d %H:%M:%S')
            > min_datetime
            }
        done_sfs = db.select_all(db.SourceFile,
                                 db.SourceFile.source == self.my_source,
                                 db.SourceFile.load_date > min_datetime)
        fpath_set -= {sf.name for sf in done_sfs}

        # Upload these archives.
        logger.info("Updating the database with %d articles." % len(fpath_set))
        self.upload_archives(db, fpath_set, n_procs=n_procs)
        return True


class Manuscripts(PmcManager):
    """ContentManager for the pmc manuscripts.

    For further details on the API, see the parent class: PmcManager.
    """
    my_path = 'pub/pmc/manuscript'
    my_source = 'manuscripts'

    def get_tarname_from_filename(self, fname):
        "Get the name of the tar file based on the file name (or a pmcid)."
        re_match = re.match('(PMC00\d).*?', fname)
        if re_match is not None:
            tarname = re_match.group(0) + 6*'X' + '.xml.tar.gz'
        else:
            tarname = None
        return tarname

    def is_archive(self, k):
        return k.endswith('.xml.tar.gz')

    def enrich_textrefs(self, db):
        """Method to add manuscript_ids to the text refs."""
        tr_list = db.select_all(db.TextRef,
                                db.TextContent.text_ref_id == db.TextRef.id,
                                db.TextContent.source == self.my_source,
                                db.TextRef.manuscript_id.is_(None))
        file_list = self.ftp.get_csv_as_dict('filelist.csv', header=0)
        pmcid_mid_dict = {entry['PMCID']: entry['MID'] for entry in file_list}
        pmid_mid_dict = {entry['PMID']: entry['MID'] for entry in file_list
                         if entry['PMID'] is not '0'}
        for tr in tr_list:
            if tr.pmcid is not None:
                tr.manuscript_id = pmcid_mid_dict[tr.pmcid]
            elif tr.pmid is not None and tr.pmid in pmid_mid_dict.keys():
                tr.manuscript_id = pmid_mid_dict[tr.pmid]
        db.commit("Could not update text refs with manuscript ids.")
        return

    @ContentManager._record_for_review
    @DGContext.wrap(gatherer, 'manuscripts')
    def update(self, db, n_procs=1):
        """Add any new content found in the archives.

        Note that this is very much the same as populating for manuscripts,
        as there are no finer grained means of getting manuscripts than just
        looking through the massive archive files. We do check to see if there
        are any new listings in each files, minimizing the amount of time
        downloading and searching, however this will in general be the slowest
        of the update methods.

        The continuing feature isn't implemented yet.
        """
        logger.info("Getting list of manuscript content available.")
        ftp_file_list = self.ftp.get_csv_as_dict('filelist.csv', header=0)
        ftp_pmcid_set = {entry['PMCID'] for entry in ftp_file_list}

        logger.info("Getting a list of text refs that already correspond to "
                    "manuscript content.")
        tr_list = db.select_all(
            db.TextRef,
            db.TextRef.id == db.TextContent.text_ref_id,
            db.TextContent.source == self.my_source
            )
        load_pmcid_set = ftp_pmcid_set - {tr.pmcid for tr in tr_list}

        logger.info("There are %d manuscripts to load."
                    % (len(load_pmcid_set)))

        logger.info("Determining which archives need to be laoded.")
        update_archives = {'PMC00%sXXXXXX.xml.tar.gz' % pmcid[3]
                           for pmcid in load_pmcid_set}

        logger.info("Beginning to upload archives.")
        self.upload_archives(db, update_archives, n_procs)
        return True


class Elsevier(ContentManager):
    """Content manager for maintaining content from Elsevier."""
    my_source = 'elsevier'
    tc_cols = ('text_ref_id', 'source', 'format', 'text_type',
               'content',)

    def __init__(self, *args, **kwargs):
        super(Elsevier, self).__init__(*args, **kwargs)
        with open(path.join(THIS_DIR, 'elsevier_titles.txt'), 'r') as f:
            self.__journal_set = {self.__regularize_title(t)
                                  for t in f.read().splitlines()}
        self.__found_journal_set = set()
        self.__matched_journal_set = set()
        return

    @staticmethod
    def __regularize_title(title):
        title = title.lower()
        for space_car in [' ', '_', '-', '.']:
            title = title.replace(space_car, '')
        return title

    def __select_elsevier_refs(self, tr_set, max_retries=2):
        """Try to check if this content is available on Elsevier."""
        elsevier_tr_set = set()
        for tr in tr_set.copy():
            if tr.doi is not None:
                publisher = get_publisher(tr.doi)
                if publisher is not None and\
                   publisher.lower() == self.my_source:
                    tr_set.remove(tr)
                    elsevier_tr_set.add(tr)

        if tr_set:
            pmid_set = {tr.pmid for tr in tr_set}
            tr_dict = {tr.pmid: tr for tr in tr_set}
            num_retries = 0
            meta_data_dict = None
            while num_retries < max_retries:
                try:
                    meta_data_dict = get_metadata_for_ids(pmid_set)
                    break
                except Exception as e:
                    num_retries += 1
                    if num_retries < max_retries:
                        logger.warning("Caught exception while getting "
                                       "metadata. Retrying...")
                    else:
                        logger.error("No more tries for:\n%s" % str(pmid_set))
                        logger.exception(e)
                        meta_data_dict = None
                        break

            if meta_data_dict is not None:
                titles = {(pmid, meta['journal_title'])
                          for pmid, meta in meta_data_dict.items()}
                for pmid, title in titles:
                    reg_title = self.__regularize_title(title)
                    self.__found_journal_set.add(reg_title)
                    if reg_title in self.__journal_set:
                        self.__matched_journal_set.add(reg_title)
                        elsevier_tr_set.add(tr_dict[pmid])
        return elsevier_tr_set

    def __get_content(self, trs):
        """Get the content."""
        article_tuples = set()
        for tr in trs:
            id_dict = {id_type: getattr(tr, id_type)
                       for id_type in ['doi', 'pmid', 'pii']
                       if getattr(tr, id_type) is not None}
            if id_dict:
                content_str = download_article_from_ids(**id_dict)
                if content_str is not None:
                    content_zip = zip_string(content_str)
                    article_tuples.add((tr.id, self.my_source, formats.TEXT,
                                        texttypes.FULLTEXT, content_zip))
        return article_tuples

    def __process_batch(self, db, tr_batch):
        logger.info("Beginning to load batch of %d text refs." % len(tr_batch))
        elsevier_trs = self.__select_elsevier_refs(tr_batch)
        logger.debug("Found %d elsevier text refs." % len(elsevier_trs))
        article_tuples = self.__get_content(elsevier_trs)
        logger.debug("Got %d elsevier results." % len(article_tuples))
        self.copy_into_db(db, 'text_content', article_tuples, self.tc_cols)
        return

    def _get_elsevier_content(self, db, tr_query, continuing=False):
        """Get the elsevier content given a text ref query object."""
        pickle_stash_fname = path.join(THIS_DIR,
                                       'checked_elsevier_trid_stash.pkl')
        tr_batch = set()
        if continuing and path.exists(pickle_stash_fname):
            with open(pickle_stash_fname, 'rb') as f:
                tr_ids_checked = pickle.load(f)
            logger.info("Continuing; %d text refs already checked."
                        % len(tr_ids_checked))
        else:
            tr_ids_checked = set()
        try:
            batch_num = 0
            for tr in tr_query.yield_per(1000):
                # If we're continuing an earlier upload, don't check id's we've
                # already checked.
                if continuing and tr.id in tr_ids_checked:
                    continue

                tr_batch.add(tr)
                if len(tr_batch) % 200 is 0:
                    batch_num += 1
                    logger.info('Beginning batch %d.' % batch_num)
                    self.__process_batch(db, tr_batch)
                    tr_ids_checked |= {tr.id for tr in tr_batch}
                    tr_batch.clear()
            if tr_batch:
                logger.info('Loading final batch.')
                self.__process_batch(db, tr_batch)
                tr_ids_checked |= {tr.id for tr in tr_batch}
        except BaseException as e:
            logger.error("Caught exception while loading elsevier.")
            logger.exception(e)
            with open(pickle_stash_fname, 'wb') as f:
                pickle.dump(tr_ids_checked, f)
            logger.info("Stashed the set of checked text ref ids in: %s"
                        % pickle_stash_fname)
            return False
        finally:
            with open('journals.pkl', 'wb') as f:
                pickle.dump({'elsevier': self.__journal_set,
                             'found': self.__found_journal_set,
                             'matched': self.__matched_journal_set}, f)
        return True

    @ContentManager._record_for_review
    def populate(self, db, n_procs=1, continuing=False):
        """Load all available elsevier content for refs with no pmc content."""
        # Note that we do not implement multiprocessing, because by the nature
        # of the web API's used, we are limited by bandwidth from any one IP.
        tr_w_pmc_q = db.filter_query(
            db.TextRef,
            db.TextRef.id == db.TextContent.text_ref_id,
            db.TextContent.text_type == 'fulltext'
            )
        tr_wo_pmc_q = db.filter_query(db.TextRef).except_(tr_w_pmc_q)
        return self._get_elsevier_content(db, tr_wo_pmc_q, continuing)

    @ContentManager._record_for_review
    def update(self, db, n_procs=1, buffer_days=15):
        """Load all available new elsevier content from new pmids."""
        # There is the possibility that elsevier content will lag behind pubmed
        # updates, so we go back a bit before the last update to make sure we
        # didn't miss anything
        latest_updatetime = self._get_latest_update(db)
        start_datetime = latest_updatetime - timedelta(days=buffer_days)

        # Construct a query for recently added (as defined above) text refs
        # that do not already have text content.
        new_trs = db.filter_query(
            db.TextRef,
            sql_exp.or_(
                db.TextRef.last_updated > start_datetime,
                db.TextRef.create_date > start_datetime,
                )
            )
        tr_w_pmc_q = db.filter_query(
            db.TextRef,
            db.TextRef.id == db.TextContent.text_ref_id,
            db.TextContent.text_type == 'fulltext'
            )
        tr_query = new_trs.except_(tr_w_pmc_q)

        return self._get_elsevier_content(db, tr_query, False)


def _make_parser():
    parser = ArgumentParser(
        description='Manage content on INDRA\'s database.'
    )
    parser.add_argument(
        choices=['upload', 'update'],
        dest='task',
        help=('Choose whether you want to perform an initial upload or update '
              'the existing content on the database.')
    )
    parser.add_argument(
        '-c', '--continue',
        dest='continuing',
        action='store_true',
        help='Continue uploading or updating, picking up where you left off.'
    )
    parser.add_argument(
        '-n', '--num_procs',
        dest='num_procs',
        type=int,
        default=1,
        help=('Select the number of processors to use during this operation. '
              'Default is 1.')
    )
    parser.add_argument(
        '-d', '--debug',
        dest='debug',
        action='store_true',
        help='Run with debugging level output.'
    )
    parser.add_argument(
        '-t', '--test',
        action='store_true',
        help='Run tests using one of the designated test databases.'
    )
    parser.add_argument(
        '-s', '--sources',
        nargs='+',
        choices=['pubmed', 'pmc_oa', 'manuscripts', 'elsevier'],
        default=['pubmed', 'pmc_oa', 'manuscripts'],
        help=('Specify which sources are to be uploaded. Defaults are pubmed, '
              'pmc_oa, and manuscripts.')
    )
    parser.add_argument(
        '-D', '--database',
        default='primary',
        help=('Choose a database from the names given in the config or '
              'environment, for example primary is INDRA_DB_PRIMAY in the '
              'config file and INDRADBPRIMARY in the environment. The default '
              'is \'primary\'. Note that this is overwridden by use of the '
              '--test flag if \'test\' is not a part of the name given.')
    )
    return parser


def _main():
    import sys
    parser = _make_parser()
    args = parser.parse_args()
    if args.debug:
        logger.setLevel(logging.DEBUG)
        from indra_db.databases import logger as db_logger
        db_logger.setLevel(logging.DEBUG)

    if not args.continuing and args.task == 'upload':
        print("#"*63)
        print(
            "# You are about to wipe the database completely clean and     #\n"
            "# load it from scratch, which could take hours, and eliminate #\n"
            "# any updates, and potentially interfere with other users.    #\n"
            "#                                                             #\n"
            "# If you wish to continue an earlier upload, add the -c       #\n"
            "# option, and if you wish to update the database, specify     #\n"
            "# `update` in your command. For mor details, use --help.      #"
        )
        print("#"*63)
        resp = input("Are you sure you want to continue? [yes/no]: ")
        if resp not in ('yes', 'y'):
            print("Aborting...")
            sys.exit()

    if args.test:
        from indra_db.tests.util import get_temp_db
        if 'test' not in args.database:
            db = get_temp_db()
        else:
            db = get_db(args.database)
    elif args.database == 'primary':
        db = get_primary_db()
    else:
        db = get_db(args.database)

    logger.info("Performing %s." % args.task)
    if args.task == 'upload':
        if not args.continuing:
            logger.info("Clearing TextContent and TextRef tables.")
            clear_succeeded = db._clear([db.TextContent, db.TextRef,
                                         db.SourceFile, db.Updates])
            if not clear_succeeded:
                sys.exit()
        for Updater in [Pubmed, PmcOA, Manuscripts, Elsevier]:
            if Updater.my_source in args.sources:
                logger.info("Populating %s." % Updater.my_source)
                Updater().populate(db, args.num_procs, args.continuing)
    elif args.task == 'update':
        for Updater in [Pubmed, PmcOA, Manuscripts, Elsevier]:
            if Updater.my_source in args.sources:
                logger.info("Updating %s." % Updater.my_source)
                Updater().update(db, args.num_procs)


if __name__ == '__main__':
    _main()
