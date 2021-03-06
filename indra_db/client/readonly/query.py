from itertools import combinations

__all__ = ['StatementQueryResult', 'QueryCore', 'Intersection', 'Union',
           'MergeQueryCore', 'HasAgent', 'FromMeshId', 'HasHash',
           'HasSources', 'HasOnlySource', 'HasReadings', 'HasDatabases',
           'SourceCore', 'SourceIntersection', 'HasType', 'IntrusiveQueryCore',
           'HasNumAgents', 'HasNumEvidence', 'FromPapers', 'EvidenceFilter']

import json
import logging
from collections import OrderedDict, Iterable, defaultdict

from sqlalchemy import desc, true, select, intersect_all, union_all, or_, \
    except_, func, null, String, and_
from sqlalchemy.dialects.postgresql import JSONB

from indra.statements import stmts_from_json, get_statement_by_name, \
    get_all_descendants
from indra_db.schemas.readonly_schema import ro_role_map, ro_type_map, \
    SOURCE_GROUPS
from indra_db.util import regularize_agent_id, get_ro

logger = logging.getLogger(__name__)


class QueryResult(object):
    """The generic result of a query.

    This class standardizes the results of queries to the readonly database.

    Parameters
    ----------
    results : dict
        The results of the query keyed by unique IDs (mk_hash for PA Statements,
        IDs for Raw Statements, etc.)
    limit : int
        The limit that was applied to this query.
    offset_comp : int
        The next offset that would be appropriate if this is a paging query.
    evidence_totals : dict
        The total numbers of evidence for each element.
    query_json : dict
        A description of the query that was used.

    Attributes
    ----------
    results : dict
        The results of the query keyed by unique IDs (mk_hash for PA Statements,
        IDs for Raw Statements, etc.)
    limit : int
        The limit that was applied to this query.
    next_offset : int
        The next offset that would be appropriate if this is a paging query.
    evidence_totals : dict
        The total numbers of evidence for each element.
    query_json : dict
        A description of the query that was used.
    """
    def __init__(self, results, limit: int, offset: int, offset_comp: int,
                 evidence_totals: dict, query_json: dict):
        if not isinstance(results, Iterable) or isinstance(results, str):
            raise ValueError("Input `results` is expected to be an iterable, "
                             "and not a string.")
        self.results = results
        self.evidence_totals = evidence_totals
        self.total_evidence = sum(self.evidence_totals.values())
        self.limit = limit
        self.offset = offset
        if limit is None or offset_comp < limit:
            self.next_offset = None
        else:
            self.next_offset = (0 if offset is None else offset) + offset_comp
        self.query_json = query_json

    def json(self) -> dict:
        """Return the JSON representation of the results."""
        if not isinstance(self.results, dict) \
                and not isinstance(self.results, list):
            json_results = list(self.results)
        else:
            json_results = self.results
        return {'results': json_results, 'limit': self.limit,
                'offset': self.offset, 'next_offset': self.next_offset,
                'query': self.query_json,
                'evidence_totals': self.evidence_totals,
                'total_evidence': self.total_evidence}


class StatementQueryResult(QueryResult):
    """The result of a query to retrieve Statements.

    This class encapsulates the results of a search for statements in the
    database. This standardizes the results of such searches.

    Attributes
    ----------
    results : dict
        The results of the query keyed by unique IDs (mk_hash for PA Statements,
        IDs for Raw Statements, etc.)
    limit : int
        The limit that was applied to this query.
    query_json : dict
        A description of the query that was used.
    """
    def __init__(self, results: dict, limit: int, offset: int,
                 evidence_totals: dict, returned_evidence: int,
                 source_counts: dict, query_json: dict):
        super(StatementQueryResult, self).__init__(results, limit,
                                                   offset, len(results),
                                                   evidence_totals, query_json)
        self.returned_evidence = returned_evidence
        self.source_counts = source_counts

    def json(self) -> dict:
        """Get the JSON dump of the results."""
        json_dict = super(StatementQueryResult, self).json()
        json_dict.update({'returned_evidence': self.returned_evidence,
                          'source_counts': self.source_counts})
        return json_dict

    def statements(self) -> list:
        """Get a list of Statements from the results."""
        return stmts_from_json(list(self.results.values()))


def _make_agent_dict(ag_dict):
    return {n: ag_dict[str(n)]
            for n in range(int(max(ag_dict.keys())) + 1)
            if str(n) in ag_dict}


class QueryCore(object):
    """The core class for all queries; not functional on its own."""

    def __init__(self, empty=False, full=False):
        if empty and full:
            raise ValueError("Cannot be both empty and full.")
        self.empty = empty
        self.full = full
        self._inverted = False

    def __repr__(self) -> str:
        args = list(self._get_constraint_json().values())[0]
        arg_strs = [f'{k}={v}' for k, v in args.items()
                    if v is not None and not k.startswith('_')]
        return f'{"~" if self._inverted else ""}{self.__class__.__name__}' \
            f'({", ".join(arg_strs)})'

    def __invert__(self):
        """Get the inverse of this object.

        q.__invert__() == ~q
        """
        # An inverted object is just a copy with a special flag added.
        inv = self.copy()
        inv._inverted = not self._inverted

        # The inverse of full is empty, and vice versa. Make sure it stays that
        # way.
        if self.full or self.empty:
            inv.full = self.empty
            inv.empty = self.full
        return inv

    def copy(self):
        """Get a _copy of this query."""
        cp = self._copy()
        cp._inverted = self._inverted
        cp.full = self.full
        cp.empty = self.empty
        return cp

    def _copy(self):
        raise NotImplementedError()

    def __hash__(self):
        return hash(str(self))

    def invert(self):
        """ A useful way to get the inversion of a query in order of operations.

        When chain operations, `~q` is evaluated after all `.` terms. This
        allows you to cleanly bypass that issue, having:

            HasReadings().invert().get_statements(ro)

        rather than

            (~HasReadings()).get_statements()

        which is harder to read.
        """
        return self.__invert__()

    def get_statements(self, ro=None, limit=None, offset=None, best_first=True,
                       ev_limit=None, evidence_filter=None) \
            -> StatementQueryResult:
        """Get the statements that satisfy this query.

        Parameters
        ----------
        ro : DatabaseManager
            A database manager handle that has valid Readonly tables built.
        limit : int
            Control the maximum number of results returned. As a rule, unless
            you are quite sure the query will result in a small number of
            matches, you should limit the query.
        offset : int
            Get results starting from the value of offset. This along with limit
            allows you to page through results.
        best_first : bool
            Return the best (most evidence) statements first.
        ev_limit : int
            Limit the number of evidence returned for each statement.
        evidence_filter : None or EvidenceFilter
            If None, no filtering will be applied. Otherwise, an EvidenceFilter
            class must be provided.

        Returns
        -------
        result : StatementQueryResult
            An object holding the JSON result from the database, as well as the
            metadata for the query.
        """
        if ro is None:
            ro = get_ro('primary')

        # If the result is by definition empty, save ourselves time and work.
        if self.empty:
            return StatementQueryResult({}, limit, offset, {}, 0, {},
                                        self.to_json())

        # Get the query for mk_hashes and ev_counts, and apply the generic
        # limits to it.
        mk_hashes_q = self.get_hash_query(ro)
        mk_hashes_q = self._apply_limits(ro, mk_hashes_q, limit, offset,
                                         best_first)

        # Do the difficult work of turning a query for hashes and ev_counts
        # into a query for statement JSONs. Return the results.
        mk_hashes_al = mk_hashes_q.subquery('mk_hashes')
        cont_q = self._get_content_query(ro, mk_hashes_al, ev_limit)
        if evidence_filter is not None:
            cont_q = evidence_filter.join_table(ro, cont_q,
                                                {'fast_raw_pa_link'})
            cont_q = evidence_filter.apply_filter(ro, cont_q)

        # If there is no evidence, whittle down the results so we only get one
        # pa_json for each hash.
        if ev_limit == 0:
            cont_q = cont_q.distinct()

        # If we have a limit on the evidence, we need to do a lateral join.
        # If we are just getting all the evidence, or none of it, just put an
        # alias on the subquery.
        if ev_limit is not None and ev_limit != 0:
            cont_q = cont_q.limit(ev_limit)
            json_content_al = cont_q.subquery().lateral('json_content')
            stmts_q = (mk_hashes_al
                       .outerjoin(json_content_al, true())
                       .outerjoin(ro.SourceMeta,
                                  ro.SourceMeta.mk_hash == mk_hashes_al.c.mk_hash))
            cols = [mk_hashes_al.c.mk_hash, ro.SourceMeta.src_json,
                    mk_hashes_al.c.ev_count, json_content_al.c.raw_json,
                    json_content_al.c.pa_json]
        else:
            json_content_al = cont_q.subquery().alias('json_content')
            stmts_q = (json_content_al
                       .outerjoin(ro.SourceMeta,
                                  ro.SourceMeta.mk_hash == json_content_al.c.mk_hash))
            cols = [json_content_al.c.mk_hash, ro.SourceMeta.src_json,
                    json_content_al.c.ev_count, json_content_al.c.raw_json,
                    json_content_al.c.pa_json]

            # Join up with other tables to pull metadata.
        stmts_q = (stmts_q
                   .outerjoin(ro.ReadingRefLink,
                              ro.ReadingRefLink.rid == json_content_al.c.rid))

        ref_link_keys = [k for k in ro.ReadingRefLink.__dict__.keys()
                         if not k.startswith('_')]

        cols += [getattr(ro.ReadingRefLink, k) for k in ref_link_keys]

        # Execute the query.
        selection = select(cols).select_from(stmts_q)

        logger.debug("Executing sql to get statements:\n%s" % str(selection))

        proxy = ro.session.connection().execute(selection)
        res = proxy.fetchall()
        if res:
            logger.debug("res is %d row by %d cols." % (len(res), len(res[0])))
        else:
            logger.debug("res is empty.")

        # Unpack the statements.
        stmts_dict = OrderedDict()
        ev_totals = OrderedDict()
        source_counts = OrderedDict()
        returned_evidence = 0
        src_list = ro.get_column_names(ro.PaStmtSrc)[1:]
        for row in res:
            # Unpack the row
            row_gen = iter(row)

            mk_hash = next(row_gen)
            src_dict = dict.fromkeys(src_list, 0)
            src_dict.update(next(row_gen))
            ev_count = next(row_gen)
            raw_json_bts = next(row_gen)
            pa_json_bts = next(row_gen)
            ref_dict = dict(zip(ref_link_keys, row_gen))

            if pa_json_bts is None:
                logger.warning("Row returned without pa_json. This likely "
                               "indicates that an over-zealous evidence filter "
                               "was used, which filtered out all evidence. "
                               "This case is not currently handled, and the "
                               "statement will have to be dropped.")
                continue

            if raw_json_bts is not None:
                returned_evidence += 1

            # Add a new statement if the hash is new.
            if mk_hash not in stmts_dict.keys():
                source_counts[mk_hash] = src_dict
                ev_totals[mk_hash] = ev_count
                stmts_dict[mk_hash] = json.loads(pa_json_bts.decode('utf-8'))
                stmts_dict[mk_hash]['evidence'] = []

            # Add annotations if not present.
            if ev_limit != 0:
                raw_json = json.loads(raw_json_bts.decode('utf-8'))
                ev_json = raw_json['evidence'][0]
                if 'annotations' not in ev_json.keys():
                    ev_json['annotations'] = {}

                # Add agents' raw text to annotations.
                ev_json['annotations']['agents'] = \
                    {'raw_text': _get_raw_texts(raw_json)}

                # Add prior UUIDs to the annotations
                if 'prior_uuids' not in ev_json['annotations'].keys():
                    ev_json['annotations']['prior_uuids'] = []
                ev_json['annotations']['prior_uuids'].append(raw_json['id'])

                # Add and/or update text refs.
                if 'text_refs' not in ev_json.keys():
                    ev_json['text_refs'] = {}
                if ref_dict['pmid']:
                    ev_json['pmid'] = ref_dict['pmid']
                elif 'PMID' in ev_json['text_refs']:
                    del ev_json['text_refs']['PMID']
                ev_json['text_refs'].update({k.upper(): v
                                             for k, v in ref_dict.items()
                                             if v is not None})

                # Add the source dictionary.
                if ref_dict['source']:
                    ev_json['annotations']['content_source'] = ref_dict['source']

                # Add the evidence JSON to the list.
                stmts_dict[mk_hash]['evidence'].append(ev_json)

        return StatementQueryResult(stmts_dict, limit, offset, ev_totals,
                                    returned_evidence, source_counts,
                                    self.to_json())

    def get_hashes(self, ro=None, limit=None, offset=None, best_first=True) \
            -> QueryResult:
        """Get the hashes of statements that satisfy this query.

        Parameters
        ----------
        ro : DatabaseManager
            A database manager handle that has valid Readonly tables built.
        limit : int
            Control the maximum number of results returned. As a rule, unless
            you are quite sure the query will result in a small number of
            matches, you should limit the query.
        offset : int
            Get results starting from the value of offset. This along with limit
            allows you to page through results.
        best_first : bool
            Return the best (most evidence) statements first.

        Returns
        -------
        result : QueryResult
            An object holding the results of the query, as well as the metadata
            for the query definition.
        """
        if ro is None:
            ro = get_ro('primary')

        # If the result is by definition empty, save time and effort.
        if self.empty:
            return QueryResult(set(), limit, offset, {}, self.to_json())

        # Get the query for mk_hashes and ev_counts, and apply the generic
        # limits to it.
        mk_hashes_q = self.get_hash_query(ro)
        mk_hashes_q = self._apply_limits(ro, mk_hashes_q, limit, offset,
                                         best_first)

        # Make the query, and package the results.
        result = mk_hashes_q.all()
        evidence_totals = {h: cnt for h, cnt in result}

        return QueryResult(list(evidence_totals.keys()), limit, offset,
                           len(result), evidence_totals, self.to_json())

    def _get_name_query(self, ro, limit=None, offset=None, best_first=True):
        mk_hashes_q = self.get_hash_query(ro)
        mk_hashes_q = self._apply_limits(ro, mk_hashes_q, limit, offset,
                                         best_first)

        mk_hashes_sq = mk_hashes_q.subquery('mk_hashes')
        q = (ro.session.query(ro.NameMeta.mk_hash, ro.NameMeta.db_id,
                              ro.NameMeta.ag_num, ro.NameMeta.type_num,
                              ro.NameMeta.agent_count, ro.NameMeta.activity,
                              ro.NameMeta.is_active, ro.SourceMeta.src_json)
             .filter(ro.NameMeta.mk_hash == mk_hashes_sq.c.mk_hash,
                     ro.SourceMeta.mk_hash == mk_hashes_sq.c.mk_hash))
        sq = q.subquery('names')
        q = ro.session.query(
            sq.c.mk_hash,
            func.jsonb_object(
                func.array_agg(sq.c.ag_num.cast(String)),
                func.array_agg(sq.c.db_id)
            ).label('agent_json'),
            sq.c.type_num,
            sq.c.agent_count,
            sq.c.activity,
            sq.c.is_active,
            sq.c.src_json.cast(JSONB).label('src_json')
        ).group_by(
            sq.c.mk_hash,
            sq.c.type_num,
            sq.c.agent_count,
            sq.c.activity,
            sq.c.is_active,
            sq.c.src_json.cast(JSONB)
        )
        return q

    def get_interactions(self, ro=None, limit=None, offset=None, best_first=True) \
            -> QueryResult:
        """Get the simple interaction information from the Statements metadata.

       Each entry in the result corresponds to a single preassembled Statement,
       distinguished by its hash.

        Parameters
        ----------
        ro : DatabaseManager
            A database manager handle that has valid Readonly tables built.
        limit : int
            Control the maximum number of results returned. As a rule, unless
            you are quite sure the query will result in a small number of
            matches, you should limit the query.
        offset : int
            Get results starting from the value of offset. This along with limit
            allows you to page through results.
        best_first : bool
            Return the best (most evidence) statements first.
        """
        if ro is None:
            ro = get_ro('primary')

        if self.empty:
            return QueryResult({}, limit, offset, {}, self.to_json())

        q = self._get_name_query(ro, limit, offset, best_first)
        names = q.all()
        results = {}
        ev_totals = {}
        for h, ag_json, type_num, n_ag, activity, is_active, src_json in names:
            results[h] = {
                'hash': h,
                'id': str(h),
                'agents': _make_agent_dict(ag_json),
                'type': ro_type_map.get_str(type_num),
                'activity': activity,
                'is_active': is_active,
                'source_counts': src_json,
            }
            ev_totals[h] = sum(src_json.values())

        return QueryResult(results, limit, offset, len(results), ev_totals,
                           self.to_json())

    def get_relations(self, ro=None, limit=None, offset=None, best_first=True,
                      with_hashes=False) \
            -> QueryResult:
        """Get the agent and type information from the Statements metadata.

         Each entry in the result corresponds to a relation, meaning an
         interaction type, and the names of the agents involved.

        Parameters
        ----------
        ro : DatabaseManager
            A database manager handle that has valid Readonly tables built.
        limit : int
            Control the maximum number of results returned. As a rule, unless
            you are quite sure the query will result in a small number of
            matches, you should limit the query.
        offset : int
            Get results starting from the value of offset. This along with limit
            allows you to page through results.
        best_first : bool
            Return the best (most evidence) statements first.
        with_hashes : bool
            Default is False. If True, retrieve all the hashes that fit within
            each relational grouping.
        """
        if ro is None:
            ro = get_ro('primary')

        if self.empty:
            return QueryResult({}, limit, offset, {}, self.to_json())

        names_q = self._get_name_query(ro, limit, offset, best_first)

        sq = names_q.subquery('names')
        q = ro.session.query(
            sq.c.agent_json,
            sq.c.type_num,
            sq.c.agent_count,
            sq.c.activity,
            sq.c.is_active,
            func.array_agg(sq.c.src_json),
            func.array_agg(sq.c.mk_hash) if with_hashes else null()
        ).group_by(
            sq.c.agent_json,
            sq.c.type_num,
            sq.c.agent_count,
            sq.c.activity,
            sq.c.is_active
        )

        names = q.all()
        results = {}
        ev_totals = {}
        num_hashes = 0
        for ag_json, type_num, n_ag, activity, is_active, srcs, hashes in names:
            ordered_agents = [ag_json.get(str(n)) for n in range(n_ag)]
            agent_key = '(' + ', '.join(str(ag) for ag in ordered_agents) + ')'

            stmt_type = ro_type_map.get_str(type_num)

            key = stmt_type + agent_key

            if key in results:
                logger.warning("Something went weird processing relations.")

            source_counts = defaultdict(lambda: 0)
            for src_json in srcs:
                for src, cnt in src_json.items():
                    source_counts[src] += cnt
            results[key] = {'id': key, 'source_counts': dict(source_counts),
                            'agents': _make_agent_dict(ag_json),
                            'type': stmt_type, 'activity': activity,
                            'is_active': is_active, 'hashes': hashes}
            ev_totals[key] = sum(source_counts.values())
            num_hashes += len(hashes)

        return QueryResult(results, limit, offset, num_hashes, ev_totals,
                           self.to_json())

    def get_agents(self, ro=None, limit=None, offset=None, best_first=True,
                   with_hashes=False) \
            -> QueryResult:
        """Get the agent pairs from the Statements metadata.

         Each entry is simply a pair (or more) of Agents involved in an
         interaction.

        Parameters
        ----------
        ro : DatabaseManager
            A database manager handle that has valid Readonly tables built.
        limit : int
            Control the maximum number of results returned. As a rule, unless
            you are quite sure the query will result in a small number of
            matches, you should limit the query.
        offset : int
            Get results starting from the value of offset. This along with limit
            allows you to page through results.
        best_first : bool
            Return the best (most evidence) statements first.
        with_hashes : bool
            Default is False. If True, retrieve all the hashes that fit within
            each agent pair grouping.
        """
        if ro is None:
            ro = get_ro('primary')

        if self.empty:
            return QueryResult({}, limit, offset, {}, self.to_json())

        names_q = self._get_name_query(ro, limit, offset, best_first)

        sq = names_q.subquery('names')
        q = ro.session.query(
            sq.c.agent_json,
            sq.c.agent_count,
            func.array_agg(sq.c.src_json),
            func.array_agg(sq.c.mk_hash) if with_hashes else null()
        ).group_by(
            sq.c.agent_json,
            sq.c.agent_count
        )
        names = q.all()

        results = {}
        ev_totals = {}
        num_hashes = 0
        for ag_json, n_ag, src_jsons, hashes in names:
            ordered_agents = [ag_json.get(str(n)) for n in range(n_ag)]
            key = 'Agents(' + ', '.join(str(ag) for ag in ordered_agents) + ')'

            if key in results:
                logger.warning("Something went weird processing results for "
                               "agents.")

            source_counts = defaultdict(lambda: 0)
            for src_json in src_jsons:
                for src, cnt in src_json.items():
                    source_counts[src] += cnt
            results[key] = {'id': key, 'source_counts': dict(source_counts),
                            'agents': _make_agent_dict(ag_json),
                            'hashes': hashes}
            ev_totals[key] = sum(source_counts.values())
            num_hashes += len(hashes)

        return QueryResult(results, limit, offset, num_hashes, ev_totals,
                           self.to_json())

    def _apply_limits(self, ro, mk_hashes_q, limit=None, offset=None,
                      best_first=True):
        """Apply the general query limits to the net hash query."""
        mk_hashes_q = mk_hashes_q.distinct()

        mk_hash_obj, ev_count_obj = self._hash_count_pair(ro)

        # Apply the general options.
        if best_first:
            mk_hashes_q = mk_hashes_q.order_by(desc(ev_count_obj))
        if limit is not None:
            mk_hashes_q = mk_hashes_q.limit(limit)
        if offset is not None:
            mk_hashes_q = mk_hashes_q.offset(offset)
        return mk_hashes_q

    def to_json(self) -> dict:
        """Get the JSON representation of this query."""
        return {'constraint': self._get_constraint_json(),
                'inverted': self._inverted}

    def _get_constraint_json(self) -> dict:
        """Get the custom constraint JSONs from the subclass"""
        raise NotImplementedError()

    def get_component_queries(self) -> list:
        """Get a list of the query elements included, in no particular order."""
        return [self.__class__.__name__]

    def _get_table(self, ro):
        raise NotImplementedError()

    def _base_query(self, ro):
        mk_hash, ev_count = self._hash_count_pair(ro)
        return ro.session.query(mk_hash.label('mk_hash'),
                                ev_count.label('ev_count'))

    def _hash_count_pair(self, ro) -> tuple:
        meta = self._get_table(ro)
        return meta.mk_hash, meta.ev_count

    def get_hash_query(self, ro, type_queries=None):
        """[Internal] Build the query for hashes."""
        # If the query is by definition everything, save much time and effort.
        if self.full:
            return ro.session.query(ro.SourceMeta.mk_hash.label('mk_hash'),
                                    ro.SourceMeta.ev_count.label('ev_count'))

        # Otherwise proceed with the usual query.
        return self._get_hash_query(ro, type_queries)

    def _get_hash_query(self, ro, inject_queries=None):
        raise NotImplementedError()

    @staticmethod
    def _get_content_query(ro, mk_hashes_al, ev_limit):
        # Incorporate a link to the JSONs in the table.
        pa_json_c = ro.FastRawPaLink.pa_json.label('pa_json')
        reading_id_c = ro.FastRawPaLink.reading_id.label('rid')
        frp_link = ro.FastRawPaLink.mk_hash == mk_hashes_al.c.mk_hash

        # If there is no evidence, don't get raw JSON, otherwise we need a col
        # for the raw JSON.
        if ev_limit == 0:
            raw_json_c = null().label('raw_json')
        else:
            raw_json_c = ro.FastRawPaLink.raw_json.label('raw_json')

        # Create the query.
        if ev_limit is None or ev_limit == 0:
            mk_hash_c = ro.FastRawPaLink.mk_hash.label('mk_hash')
            ev_count_c = mk_hashes_al.c.ev_count.label('ev_count')
            cont_q = ro.session.query(mk_hash_c, ev_count_c, raw_json_c,
                                      pa_json_c, reading_id_c)
        else:
            cont_q = ro.session.query(raw_json_c, pa_json_c, reading_id_c)
        cont_q = cont_q.filter(frp_link)

        return cont_q

    def __merge_queries(self, other, MergeClass):
        """This is the most general method for handling query merges.

        That is to say, for handling __and__ and __or__ calls.
        """
        # We cannot merge with things that aren't queries.
        if not isinstance(other, QueryCore):
            raise ValueError(f"{self.__class__.__name__} cannot operate with "
                             f"{type(other)}")

        # If this and/or the other is a merged query, special handling ensures
        # the result is efficient. Otherwise, just create a new merged query.
        if isinstance(self, MergeClass):
            if isinstance(other, MergeClass):
                return MergeClass(self.queries[:] + other.queries[:])
            else:
                return MergeClass(self.queries[:] + (other.copy(),))
        elif isinstance(other, MergeClass):
            return MergeClass(other.queries[:] + (self.copy(),))
        else:
            return MergeClass([other.copy(), self.copy()])

    def _do_and(self, other):
        """Sub-method of __and__ that can be over-written by child classes."""
        return self.__merge_queries(other, Intersection)

    def __and__(self, other):
        # Dismiss the trivial case where two queries are the same.
        if self == other:
            return self.copy()

        # Handle the case where one of the queries is full, but not the other.
        if self.full and not other.full:
            return other.copy()
        elif other.full and self.full:
            return self.copy()

        return self._do_and(other)

    def _do_or(self, other):
        """Sub-method of __or__ that can be over-written by chile classes."""
        return self.__merge_queries(other, Union)

    def __or__(self, other):
        # Dismiss the trivial case where two queries are the same.
        if self == other:
            return self.copy()

        # If one of the queries is empty, but not the other, dismiss them:
        if self.empty and not other.empty:
            return other.copy()
        elif other.empty and not self.empty:
            return self.copy()

        return self._do_or(other)

    def _merge_lists(self, is_and, other, fallback):
        if isinstance(other, self.__class__) \
                and self._inverted == other._inverted:
            # Two type queries of the same polarity can be merged, with some
            # care for whether they are both inverted or not.
            my_set = set(self._get_list())
            yo_set = set(other._get_list())
            if not self._inverted:
                merged_values = my_set & yo_set if is_and else my_set | yo_set
                empty = len(merged_values) == 0
                full = False
            else:
                # RDML
                merged_values = my_set | yo_set if is_and else my_set & yo_set
                full = len(merged_values) == 0
                empty = False
            res = self.__class__(merged_values)
            res._inverted = self._inverted
            res.full = full
            res.empty = empty
            return res
        elif self.is_inverse_of(other):
            # If the two queries are inverses, we can simply return a empty
            # result trivially. (A and not A is nothing)
            return self._get_empty() if is_and else ~self._get_empty()

        return fallback(other)

    def __sub__(self, other):
        # Subtraction is the same as "and not"
        return self._do_and(~other)

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False
        return self.to_json() == other.to_json()

    def is_inverse_of(self, other):
        """Check if a query is the exact opposite of another."""
        if not isinstance(other, self.__class__):
            return False
        if not self._get_constraint_json() == other._get_constraint_json():
            return False
        return self._inverted != other._inverted


class EmptyQuery:
    def __and__(self, other):
        if not isinstance(other, QueryCore):
            raise TypeError(f"Cannot perform __and__ operation with "
                            f"{type(other)} and EmptyQuery.")
        return other

    def __or__(self, other):
        if not isinstance(other, QueryCore):
            raise TypeError(f"Cannot perform __or__ operation with "
                            f"{type(other)} and EmptyQuery.")
        return other

    def __sub__(self, other):
        if not isinstance(other, QueryCore):
            raise TypeError(f"Cannot perform __sub__ operation with "
                            f"{type(other)} and EmptyQuery.")
        return other.invert()

    def __eq__(self, other):
        if isinstance(other, EmptyQuery):
            return True
        return False


class SourceCore(QueryCore):
    """The core of all queries that use SourceMeta."""

    def _get_constraint_json(self) -> dict:
        raise NotImplementedError()

    def _do_and(self, other) -> QueryCore:
        # Make sure that intersections of SourceCore children end up in
        # SourceIntersection.
        if isinstance(other, SourceCore):
            return SourceIntersection([self.copy(), other.copy()])
        elif isinstance(other, SourceIntersection):
            return SourceIntersection(other.source_queries + (self.copy(),))
        return super(SourceCore, self)._do_and(other)

    def _copy(self) -> QueryCore:
        raise NotImplementedError()

    def _get_table(self, ro):
        return ro.SourceMeta

    def _apply_filter(self, ro, query, invert=False):
        raise NotImplementedError()

    def _get_hash_query(self, ro, inject_queries=None):
        q = self._base_query(ro)
        q = self._apply_filter(ro, q)
        if inject_queries is not None:
            for type_q in inject_queries:
                q = type_q._apply_filter(self._get_table(ro), q)
        return q


class SourceIntersection(QueryCore):
    """A special type of intersection between children of SourceCore.

    All SourceCore queries use the same table, so when doing an intersection it
    doesn't make sense to do an actual intersection operation, and instead
    simply apply all the filters of each query to build a normal multi-
    conditioned query.
    """
    def __init__(self, source_queries):
        # There are several points at which we could realize this query is by
        # definition empty.
        empty = False

        # Look through all the queries, picking out special cases and grouping
        # the rest by class.
        add_hashes = None
        rem_hashes = set()
        class_groups = defaultdict(list)
        for sq in source_queries:
            if isinstance(sq, HasHash):
                # Collect all hashes to include and those to exclude.
                if not sq._inverted:
                    # This is a part of an intersection, so intersection is
                    # appropriate.
                    if add_hashes is None:
                        add_hashes = set(sq.stmt_hashes)
                    else:
                        add_hashes &= set(sq.stmt_hashes)
                else:
                    # This follows form De Morgan's Law
                    rem_hashes |= set(sq.stmt_hashes)
            else:
                # We will need to check other class groups for inversion, so
                # group them now for efficiency.
                class_groups[sq.__class__].append(sq)

        # Start building up the true set of queries.
        filtered_queries = set()

        # Add the hash queries.
        if add_hashes and rem_hashes and add_hashes == rem_hashes:
            # In this special case I am empty, and to make sure my inversion
            # works smoothly, I keep these two queries around so the Union can
            # successfully work out the logic without special communication
            # being necessary.
            empty = True
            filtered_queries |= {HasHash(add_hashes),
                                 ~HasHash(rem_hashes)}
        else:
            # Check for added hashes and add a positive and an inverted hash
            # query for the net positive and net negative hashes.
            if add_hashes is not None:
                if not add_hashes:
                    empty = True
                filtered_queries.add(HasHash(add_hashes - rem_hashes))
                rem_hashes -= add_hashes

            if rem_hashes:
                filtered_queries.add(~HasHash(rem_hashes))

        # Now add in all the other queries, removing those that cancel out.
        for q_list in class_groups.values():
            if len(q_list) == 1:
                filtered_queries.add(q_list[0])
            else:
                filtered_queries |= set(q_list)
                if not empty:
                    for q1, q2 in combinations(q_list, 2):
                        if q1.is_inverse_of(q2):
                            empty = True
                            break

        # Make the source queries a tuple, thus immutable.
        self.source_queries = tuple(filtered_queries)

        # I am empty if any of my queries is empty, or if I have no queries.
        empty |= any(q.empty for q in self.source_queries)
        empty |= len(self.source_queries) == 0
        super(SourceIntersection, self).__init__(empty)

    def _copy(self):
        return self.__class__(self.source_queries)

    def __invert__(self):
        return Union([~q for q in self.source_queries])

    def _do_and(self, other):
        # This is the complement of _do_and in SourceCore, together ensuring
        # that any intersecting group of Source queries goes into this class.
        if isinstance(other, SourceIntersection):
            return SourceIntersection(self.source_queries
                                      + other.source_queries)
        elif isinstance(other, SourceCore):
            return SourceIntersection(self.source_queries + (other.copy(),))
        return super(SourceIntersection, self)._do_and(other)

    def __str__(self):
        str_list = [str(sq) for sq in self.source_queries]
        if not self._inverted:
            return ' and '.join(str_list)
        else:
            return 'not (' + ' and not '.join(str_list) + ')'

    def __repr__(self):
        query_reprs = [repr(q) for q in self.source_queries]
        return f'{self.__class__.__name__}([{", ".join(query_reprs)}])'

    def _get_constraint_json(self) -> dict:
        query_list = [q.to_json() for q in self.source_queries]
        return {'multi_source_query': {'source_queries': query_list}}

    def get_component_queries(self) -> list:
        return [q.__class__.__name__ for q in self.source_queries] \
               + [self.__class__.__name__]

    def _get_table(self, ro):
        return ro.SourceMeta

    def _get_hash_query(self, ro, inject_queries=None):
        query = self._base_query(ro)

        # Apply each of the source queries' filters.
        for sq in self.source_queries:
            query = sq._apply_filter(ro, query, self._inverted)

        # Apply any type queries.
        if inject_queries:
            for tq in inject_queries:
                query = tq._apply_filter(self._get_table(ro), query)
        return query


class HasOnlySource(SourceCore):
    """Find Statements that come exclusively from a particular source.

    For example, find statements that come only from sparser.

    Parameters
    ----------
    only_source : str
        The only source that spawned the statement, e.g. signor, or reach.
    """
    def __init__(self, only_source):
        self.only_source = only_source
        super(HasOnlySource, self).__init__()

    def __str__(self):
        invert_mod = 'not ' if self._inverted else ''
        return f"is {invert_mod}only from {self.only_source}"

    def _copy(self):
        return self.__class__(self.only_source)

    def _get_constraint_json(self) -> dict:
        return {'has_only_source': {'only_source': self.only_source}}

    def ev_filter(self):
        if not self._inverted:
            def get_clause(ro):
                return ro.RawStmtSrc.src == self.only_source
        else:
            def get_clause(ro):
                return ro.RawStmtSrc.src != self.only_source
        return EvidenceFilter.from_filter('raw_stmt_src', get_clause)

    def _apply_filter(self, ro, query, invert=False):
        inverted = self._inverted ^ invert
        meta = self._get_table(ro)
        if not inverted:
            clause = meta.only_src.like(self.only_source)
        else:
            clause = meta.only_src.is_distinct_from(self.only_source)
        return query.filter(clause)


class HasSources(SourceCore):
    """Find Statements that include a set of sources.

    For example, find Statements that have support from both medscan and reach.

    Parameters
    ----------
    sources : list or set or tuple
        A collection of strings, each string the canonical name for a source.
        The result will include statements that have evidence from ALL sources
        that you include.
    """
    def __init__(self, sources):
        empty = False
        if len(sources) == 0:
            empty = True
        self.sources = tuple(set(sources))
        super(HasSources, self).__init__(empty)

    def _copy(self):
        return self.__class__(self.sources)

    def __str__(self):
        if not self._inverted:
            return f"is from all of {self.sources}"
        else:
            return f"is not from one of {self.sources}"

    def _get_constraint_json(self) -> dict:
        return {'has_sources': {'sources': self.sources}}

    def ev_filter(self):
        if not self._inverted:
            def get_clause(ro):
                return ro.RawStmtSrc.src.in_(self.sources)
        else:
            def get_clause(ro):
                return ro.RawStmtSrc.src.notin_(self.sources)
        return EvidenceFilter.from_filter('raw_stmt_src', get_clause)

    def _apply_filter(self, ro, query, invert=False):
        inverted = self._inverted ^ invert
        meta = self._get_table(ro)
        clauses = []
        for src in self.sources:
            if not inverted:
                clauses.append(getattr(meta, src) > 0)
            else:
                # Careful here: lacking a source makes the cell null, not 0.
                clauses.append(getattr(meta, src).is_(None))
        if not inverted:
            query = query.filter(*clauses)
        else:
            # Recall De Morgan's Law.
            query = query.filter(or_(*clauses))
        return query


class SourceTypeCore(SourceCore):
    """The base class for HasReadings and HasDatabases."""
    name = NotImplemented
    col = NotImplemented

    def __init__(self):
        super(SourceTypeCore, self).__init__()

    def __str__(self):
        if not self._inverted:
            return f"has {self.name}"
        else:
            return f"has no {self.name}"

    def _copy(self):
        return self.__class__()

    def _get_constraint_json(self) -> dict:
        return {f'has_{self.name}_query': {f'_has_{self.name}': True}}

    def ev_filter(self):
        if self.col == 'has_rd':
            my_src_group = SOURCE_GROUPS['reading']
        elif self.col == 'has_db':
            my_src_group = SOURCE_GROUPS['databases']
        else:
            raise RuntimeError("`col` class attribute not recognized.")

        if not self._inverted:
            def get_clause(ro):
                return ro.RawStmtSrc.src.in_(my_src_group)
        else:
            def get_clause(ro):
                return ro.RawStmtSrc.src.notin_(my_src_group)

        return EvidenceFilter.from_filter('raw_stmt_src', get_clause)

    def _apply_filter(self, ro, query, invert=False):
        inverted = self._inverted ^ invert
        meta = self._get_table(ro)

        # In raw SQL, you can simply say "WHERE has_rd", for example, if it is
        # boolean. I would like to see if I can do that here...might speed
        # things up.
        if not inverted:
            clause = getattr(meta, self.col) == True
        else:
            clause = getattr(meta, self.col) == False
        return query.filter(clause)


class HasReadings(SourceTypeCore):
    """Find Statements that have readings."""
    name = 'readings'
    col = 'has_rd'


class HasDatabases(SourceTypeCore):
    """Find Statements that have databases."""
    name = 'databases'
    col = 'has_db'


class HasHash(SourceCore):
    """Find Statements from a list of hashes.

    Parameters
    ----------
    stmt_hashes : list or set or tuple
        A collection of integers, where each integer is a shallow matches key
        hash of a Statement (frequently simply called "mk_hash" or "hash")
    """
    list_name = 'stmt_hashes'

    def __init__(self, stmt_hashes):
        empty = len(stmt_hashes) == 0
        self.stmt_hashes = tuple(stmt_hashes)
        super(HasHash, self).__init__(empty)

    def _copy(self):
        return self.__class__(self.stmt_hashes)

    def __str__(self):
        return f"hash {'not ' if self._inverted else ''}in {self.stmt_hashes}"

    def _get_constraint_json(self) -> dict:
        return {"hash_query": {'hashes': list(self.stmt_hashes)}}

    def _get_empty(self):
        return self.__class__([])

    def _get_list(self):
        return getattr(self, self.list_name)

    def _do_and(self, other) -> QueryCore:
        return self._merge_lists(True, other, super(HasHash, self)._do_and)

    def _do_or(self, other) -> QueryCore:
        return self._merge_lists(False, other, super(HasHash, self)._do_or)

    def _apply_filter(self, ro, query, invert=False):
        inverted = self._inverted ^ invert
        mk_hash, _ = self._hash_count_pair(ro)
        if len(self.stmt_hashes) == 1:
            # If there is only one hash, use equalities (faster)
            if not inverted:
                clause = mk_hash == self.stmt_hashes[0]
            else:
                clause = mk_hash != self.stmt_hashes[0]
        else:
            # Otherwise use "in"s.
            if not inverted:
                clause = mk_hash.in_(self.stmt_hashes)
            else:
                clause = mk_hash.notin_(self.stmt_hashes)
        return query.filter(clause)


class HasAgent(QueryCore):
    """Get Statements that have a particular agent in a particular role.

    Parameters
    ----------
    agent_id : str
        The ID string naming the agent, for example 'ERK' (FPLX or NAME) or
        'plx' (TEXT), and so on.
    namespace : str
        (optional) By default, this is NAME, indicating the canonical name of
        the agent. Other options for namespace include FPLX (FamPlex), CHEBI,
        CHEMBL, HGNC, UP (UniProt), TEXT (for raw text mentions), and many more.
    role : str or None
        (optional) None by default. Options are "SUBJECT", "OBJECT", or "OTHER".
    agent_num : int or None
        (optional) None by default. The regularized position of the agent in the
        Statement's list of agents.
    """
    def __init__(self, agent_id, namespace='NAME', role=None, agent_num=None):
        self.agent_id = agent_id
        self.namespace = namespace

        if role is not None and agent_num is not None:
            raise ValueError("Only specify role OR agent_num, not both.")

        self.role = role
        self.agent_num = agent_num

        # Regularize ID based on Database optimization (e.g. striping prefixes)
        self.regularized_id = regularize_agent_id(agent_id, namespace)
        super(HasAgent, self).__init__()

    def _copy(self):
        return self.__class__(self.agent_id, self.namespace, self.role,
                              self.agent_num)

    def __str__(self):
        s = 'not ' if self._inverted else ''
        s += f"has an agent where {self.namespace} = {self.agent_id}"
        if self.role is not None:
            s += f" with role={self.role}"
        elif self.agent_num is not None:
            s += f" with agent_num={self.agent_num}"
        return s

    def _get_constraint_json(self) -> dict:
        return {'agent_query': {'agent_id': self.agent_id,
                                'namespace': self.namespace,
                                '_regularized_id': self.regularized_id,
                                'role': self.role,
                                'agent_num': self.agent_num}}

    def _get_table(self, ro):
        # The table used depends on the namespace.
        if self.namespace == 'NAME':
            meta = ro.NameMeta
        elif self.namespace == 'TEXT':
            meta = ro.TextMeta
        else:
            meta = ro.OtherMeta
        return meta

    def _get_hash_query(self, ro, inject_queries=None):
        # Get the base query and filter by regularized ID.
        meta = self._get_table(ro)
        qry = self._base_query(ro).filter(meta.db_id.like(self.regularized_id))

        # If we aren't going to one of the special tables for NAME or TEXT, we
        # need to filter by namespace.
        if self.namespace not in ['NAME', 'TEXT', None]:
            qry = qry.filter(meta.db_name.like(self.namespace))

        # Convert the role to a number for faster lookup, or else apply
        # agent_num.
        if self.role is not None:
            role_num = ro_role_map.get_int(self.role)
            qry = qry.filter(meta.role_num == role_num)
        elif self.agent_num is not None:
            qry = qry.filter(meta.ag_num == self.agent_num)

        # Apply the type searches, and invert if needed..
        if not self._inverted:
            if inject_queries:
                for tq in inject_queries:
                    qry = tq._apply_filter(self._get_table(ro), qry)
        else:
            # Inversion in this case requires using an "except" clause, because
            # each hash is represented by multiple agents.
            if inject_queries:
                # which does mean the Application of De Morgan's law is tricky
                # here, but apply it we must.
                type_clauses = [tq.invert()._get_clause(self._get_table(ro))
                                for tq in inject_queries]
                qry = self._base_query(ro).filter(or_(qry.whereclause,
                                                      *type_clauses))
            al = except_(self._base_query(ro), qry).alias('agent_exclude')
            qry = ro.session.query(al.c.mk_hash.label('mk_hash'),
                                   al.c.ev_count.label('ev_count'))

        return qry


class FromPapers(QueryCore):
    """Find Statements that have evidence from particular papers.

    Parameters
    ----------
    paper_list : list[(<id_type>, <paper_id>)]
        A list of tuples, where each tuple indicates and id-type (e.g. 'pmid')
        and an id value for a particular paper.

    Returns
    -------
    A dictionary data structure containing, among other metadata, a dict of
    statement jsons under the key 'statements', themselves keyed by their
    shallow matches-key hashes.
    """
    list_name = 'paper_list'

    def __init__(self, paper_list):
        self.paper_list = tuple(set(paper_list))
        super(FromPapers, self).__init__(len(self.paper_list) == 0)

    def __str__(self) -> str:
        ret = 'not ' if self._inverted else ''
        return ret + f"from papers {self.paper_list}"

    def _copy(self) -> QueryCore:
        return self.__class__(self.paper_list)

    def _get_empty(self):
        return self.__class__([])

    def _get_list(self):
        return getattr(self, self.list_name)

    def _do_and(self, other) -> QueryCore:
        return self._merge_lists(True, other, super(FromPapers, self)._do_and)

    def _do_or(self, other) -> QueryCore:
        return self._merge_lists(False, other, super(FromPapers, self)._do_or)

    def _get_constraint_json(self) -> dict:
        return {'from_papers': {'paper_list': self.paper_list}}

    def _get_table(self, ro):
        return ro.EvidenceCounts

    def _get_conditions(self, ro):
        conditions = []
        for id_type, paper_id in self.paper_list:
            if paper_id is None:
                logger.warning("Got paper with id None.")
                continue

            # TODO: upgrade this to use new id formatting. This will require
            # updating the ReadingRefLink table in the readonly build.
            tbl_attr = getattr(ro.ReadingRefLink, id_type)
            if not self._inverted:
                if id_type in ['trid', 'tcid']:
                    conditions.append(tbl_attr == int(paper_id))
                else:
                    conditions.append(tbl_attr.like(paper_id))
            else:
                if id_type in ['trid', 'tcid']:
                    conditions.append(tbl_attr != int(paper_id))
                else:
                    conditions.append(tbl_attr.notlike(paper_id))
        return conditions

    def _get_hash_query(self, ro, inject_queries=None):
        # Create a sub-query on the reading metadata
        q = ro.session.query(ro.ReadingRefLink.rid.label('rid'))
        conditions = self._get_conditions(ro)
        if not self._inverted:
            q = q.filter(or_(*conditions))
        else:
            # RDML (implicit "and")
            q = q.filter(*conditions)

        sub_al = q.subquery('reading_ids')

        # Map the reading metadata query to mk_hashes with statement counts.
        qry = (self._base_query(ro)
               .filter(ro.EvidenceCounts.mk_hash == ro.FastRawPaLink.mk_hash,
                       ro.FastRawPaLink.reading_id == sub_al.c.rid))
        return qry

    def ev_filter(self):
        if not self._inverted:
            def get_clause(ro):
                return or_(*self._get_conditions(ro))
        else:
            def get_clause(ro):
                return and_(*self._get_conditions(ro))
        return EvidenceFilter.from_filter('reading_ref_link', get_clause)


class IntrusiveQueryCore(QueryCore):
    """This is the parent of all queries that draw on info in all meta tables.

    Thus, when using these queries in an Intersection, they are applied to each
    sub query separately.
    """
    name = NotImplemented
    list_name = NotImplemented
    item_type = NotImplemented
    col_name = NotImplemented

    def __init__(self, value_list):
        value_tuple = tuple([self.item_type(n) for n in value_list])
        setattr(self, self.list_name, value_tuple)
        super(IntrusiveQueryCore, self).__init__(len(value_tuple) == 0)

    def _get_empty(self) -> QueryCore:
        return self.__class__([])

    def _copy(self) -> QueryCore:
        return self.__class__(self._get_list())

    def _get_list(self):
        return getattr(self, self.list_name)

    def _do_and(self, other) -> QueryCore:
        return self._merge_lists(True, other,
                                 super(IntrusiveQueryCore, self)._do_and)

    def _do_or(self, other) -> QueryCore:
        return self._merge_lists(False, other,
                                 super(IntrusiveQueryCore, self)._do_or)

    def _get_constraint_json(self) -> dict:
        return {self.name: {self.list_name: list(self._get_list())}}

    def _get_table(self, ro):
        return ro.SourceMeta

    def _get_query_values(self):
        # This method can be subclassed in case values need to be processed
        # before the query, a la HasType
        return self._get_list()

    def _get_clause(self, meta):
        q_values = self._get_query_values()
        col = getattr(meta, self.col_name)
        if len(q_values) == 1:
            if not self._inverted:
                clause = col == q_values[0]
            else:
                clause = col != q_values[0]
        else:
            if not self._inverted:
                clause = col.in_(q_values)
            else:
                clause = col.notin_(q_values)
        return clause

    def _apply_filter(self, meta, query):
        """Apply the filter to the query.

        Defined generically for application by other classes when included
        in an Intersection.
        """
        return query.filter(self._get_clause(meta))

    def _get_hash_query(self, ro, inject_queries=None):
        if inject_queries is not None \
                and any(q.name == self.name for q in inject_queries):
            raise ValueError(f"Cannot apply {self.name} queries to another "
                             f"{self.name} query.")
        q = self._apply_filter(self._get_table(ro), self._base_query(ro))
        if inject_queries is not None:
            for other_in_q in inject_queries:
                q = other_in_q._apply_filter(self._get_table(ro), q)
        return q


class HasNumAgents(IntrusiveQueryCore):
    """Find Statements with any one of a listed number of agents.

     For example, `HasNumAgents([1,3,4])` will return agents with either 2,
     3, or 4 agents (the latter two mostly being complexes).

    NOTE: when used in an Interaction with other queries, the agent numbers are
    handled specially, with each sub-query having an agent_count constraint
    applied to it.

    Parameters
    ----------
    agent_nums : tuple
        A list of integers, each indicating a number of agents.
    """
    name = 'has_num_agents'
    list_name = 'agent_nums'
    item_type = int
    col_name = 'agent_count'

    def __init__(self, agent_nums):
        super(HasNumAgents, self).__init__(agent_nums)
        if 0 in self.agent_nums:
            raise ValueError(f"Each element of {self.list_name} must be "
                             f"greater than 0.")

    def __str__(self):
        invert_word = 'not ' if self._inverted else ''
        return f"number of agents {invert_word}in {self.agent_nums}"


class HasNumEvidence(IntrusiveQueryCore):
    """Find Statements with one of a given number of evidence.

    For example, HasNumEvidence([2,3,4]) will return Statements that have
    either 2, 3, or 4 evidence.

    NOTE: when used in an Interaction with other queries, the evidence count is
    handled specially, with each sub-query having an ev_count constraint
    added to it.

    Parameters
    ----------
    evidence_nums : tuple
        A list of numbers greater than 0, each indicating a number of evidence.
    """
    name = 'has_num_evidence'
    list_name = 'evidence_nums'
    item_type = int
    col_name = 'ev_count'

    def __init__(self, evidence_nums):
        super(HasNumEvidence, self).__init__(evidence_nums)
        if 0 in self.evidence_nums:
            raise ValueError("Each Statement must have at least one Evidence.")

    def __str__(self):
        invert_word = 'not ' if self._inverted else ''
        return f"number of evidence {invert_word}in {self.evidence_nums}"


class HasType(IntrusiveQueryCore):
    """Find Statements that are one of a collection of types.

    For example, you can find Statements that are Phosphorylations or
    Activations, or you could find all subclasses of RegulateActivity.

    NOTE: when used in an Intersection with other queries, type is handled
    specially, with each sub query having a type constraint added to it.

    Parameters
    ----------
    stmt_types : set or list or tuple
        A collection of Strings, where each string is a class name for a type
        of Statement. Spelling and capitalization are necessary.
    include_subclasses : bool
        (optional) default is False. If True, each Statement type given in the
        list will be expanded to include all of its sub classes.
    """
    name = 'has_type'
    list_name = 'stmt_types'
    item_type = str
    col_name = 'type_num'

    def __init__(self, stmt_types, include_subclasses=False):
        # Do the expansion of sub classes, if requested.
        st_set = set(stmt_types)
        if include_subclasses:
            for stmt_type in stmt_types:
                stmt_class = get_statement_by_name(stmt_type)
                sub_classes = get_all_descendants(stmt_class)
                st_set |= {c.__name__ for c in sub_classes}
        super(HasType, self).__init__(st_set)

    def __str__(self):
        invert_word = 'not ' if self._inverted else ''
        return f"type {invert_word}in {self.stmt_types}"

    def _get_query_values(self):
        return [ro_type_map.get_int(st) for st in self.stmt_types]


class FromMeshId(QueryCore):
    """Find Statements whose text sources were given a particular MeSH ID.

    Parameters
    ----------
    mesh_id : str
        A canonical MeSH ID, of the "D" variety, e.g. "D000135".
    """
    def __init__(self, mesh_id):
        if not mesh_id.startswith('D') and not mesh_id[1:].isdigit():
            raise ValueError("Invalid MeSH ID: %s. Must begin with 'D' and "
                             "the rest must be a number." % mesh_id)
        self.mesh_id = mesh_id
        self.mesh_num = int(mesh_id[1:])
        super(FromMeshId, self).__init__()

    def __str__(self):
        invert_char = '!' if self._inverted else ''
        return f"MeSH ID {invert_char}= {self.mesh_id}"

    def _copy(self):
        return self.__class__(self.mesh_id)

    def _get_constraint_json(self) -> dict:
        return {'mesh_query': {'mesh_id': self.mesh_id,
                               '_mesh_num': self.mesh_num}}

    def _get_table(self, ro):
        return ro.MeshMeta

    def _get_hash_query(self, ro, inject_queries=None):
        meta = self._get_table(ro)
        qry = self._base_query(ro).filter(meta.mesh_num == self.mesh_num)

        if not self._inverted:
            if inject_queries:
                for tq in inject_queries:
                    qry = tq._apply_filter(self._get_table(ro), qry)
        else:
            # For much the same reason as with agent queries, an `except_` is
            # required to perform inversion. Also likewise, great care is
            # required to handle the type queries.
            new_base = ro.session.query(
                ro.SourceMeta.mk_hash.label('mk_hash'),
                ro.SourceMeta.ev_count.label('ev_count')
            )
            if inject_queries:
                for tq in inject_queries:
                    new_base = tq._apply_filter(ro.SourceMeta, new_base)

            al = except_(new_base, qry).alias('mesh_exclude')
            qry = ro.session.query(al.c.mk_hash.label('mk_hash'),
                                   al.c.ev_count.label('ev_count'))
        return qry

    def ev_filter(self):
        if not self._inverted:
            def get_clause(ro):
                return ro.RawStmtMesh.mesh_num == self.mesh_num
        else:
            def get_clause(ro):
                return ro.RawStmtMesh.mesh_num.is_distinct_from(self.mesh_num)

        return EvidenceFilter.from_filter('raw_stmt_mesh', get_clause)


class MergeQueryCore(QueryCore):
    """This is the parent of the two merge classes: Intersection and Union.

    This class of queries is extremely special, in that the "table" is actually
    constructed on the fly. This presents various subtle challenges. Moreover
    an intersection/union is an expensive process, so I go to great lengths to
    minimize its use, making the __init__ methods quite hefty. It is also in
    Intersections and Unions that `full` and `empty` states are most likely to
    occur, and in some wonderfully subtle and hard to find ways.
    """
    join_word = NotImplemented
    name = NotImplemented

    def __init__(self, query_list, *args, **kwargs):
        # Make the collection of queries immutable.
        self.queries = tuple(query_list)

        # This variable is used internally during the construction of the
        # joint query.
        self._injected_queries = None

        # Because of the derivative nature of the "tables" involved, some more
        # dynamism is required to get, for instance, the hash and count pair.
        self._mk_hashes_al = None
        super(MergeQueryCore, self).__init__(*args, **kwargs)

    def __invert__(self):
        raise NotImplementedError()

    def _copy(self):
        return self.__class__(self.queries)

    def _get_table(self, ro):
        raise NotImplementedError()

    @staticmethod
    def _merge(*queries):
        raise NotImplementedError()

    def __str__(self):
        query_strs = []
        for q in self.queries:
            if isinstance(q, MergeQueryCore) or q._inverted:
                query_strs.append(f"({q})")
            else:
                query_strs.append(str(q))
        ret = f' {self.join_word} '.join(query_strs)
        return ret

    def __repr__(self):
        query_strs = [repr(q) for q in self.queries]
        return f'{self.__class__.__name__}([{", ".join(query_strs)}])'

    def _get_constraint_json(self) -> dict:
        return {f'{self.name}_query': [q.to_json() for q in self.queries]}

    def get_component_queries(self) -> list:
        type_list = []
        for q in self.queries:
            type_list += q.get_component_queries()
        return type_list + [self.__class__.__name__]

    def _hash_count_pair(self, ro) -> tuple:
        mk_hashes_al = self._get_table(ro)
        return mk_hashes_al.c.mk_hash, mk_hashes_al.c.ev_count

    def _get_hash_query(self, ro, inject_queries=None):
        self._injected_queries = inject_queries
        self._mk_hashes_al = None  # recalculate the join
        try:
            qry = self._base_query(ro)
        finally:
            self._injected_queries = None
        return qry


class Intersection(MergeQueryCore):
    """The Intersection of multiple queries.

    Baring special handling, this is what results from q1 & q2.

    NOTE: the inverse of an Intersection is a Union (De Morgans's Law)
    """
    name = 'intersection'
    join_word = 'and'

    def __init__(self, query_list):
        # Look for groups of queries that can be merged otherwise, and gather
        # up the type queries for special handling. Also, check to see if any
        # queries are empty, in which case the net query is necessarily empty.
        mergeable_query_types = [SourceIntersection, SourceCore, FromPapers]
        mergeable_groups = {C: [] for C in mergeable_query_types}
        query_groups = defaultdict(list)
        filtered_queries = set()
        self._in_queries = {'pos': {}, 'neg': {}}
        empty = False
        all_full = True
        for query in query_list:
            if query.empty:
                empty = True
            if not query.full:
                all_full = False
            for C in mergeable_query_types:
                # If this is any kind of source query, add it to a list to be
                # merged with its own kind.
                if isinstance(query, C):
                    mergeable_groups[C].append(query)
                    break
            else:
                if isinstance(query, IntrusiveQueryCore):
                    # Extract the intrusive (type, agent number, evidence
                    # number) queries, and merge them together as much as
                    # possible.
                    name = query.name
                    if not query._inverted:
                        if name not in self._in_queries['pos']:
                            self._in_queries['pos'][name] = query
                        else:
                            self._in_queries['pos'][name] &= query
                    else:
                        if name not in self._in_queries['neg']:
                            self._in_queries['neg'][name] = query
                        else:
                            self._in_queries['neg'][name] &= query
                else:
                    query_groups[query.__class__].append(query)
                filtered_queries.add(query)

        # Add mergeable queries into the final set.
        for queries in mergeable_groups.values():
            if len(queries) == 1:
                filtered_queries.add(queries[0])
            elif len(queries) > 1:
                # Merge the queries based on their inversion.
                pos_query = None
                neg_query = None
                for q in queries:
                    if not q._inverted:
                        if pos_query is None:
                            pos_query = q
                        else:
                            pos_query &= q
                    else:
                        if neg_query is None:
                            neg_query = q
                        else:
                            neg_query &= q

                # Add the merged query to the final set.
                for query in [neg_query, pos_query]:
                    if query is not None:
                        filtered_queries.add(query)
                        query_groups[query.__class__].append(query)

        # Look for exact contradictions (any one of which makes this empty).
        # Also make sure there is no empty-inducing interaction between my
        # type queries and the Unions.
        if not empty:
            for cls, q_list in query_groups.items():
                # Simply check for exact contradictions.
                if len(q_list) > 1:
                    for q1, q2 in combinations(q_list, 2):
                        if q1.is_inverse_of(q2):
                            empty = True

                # Special care is needed to make sure my intrusive queries
                # don't identically wipe out everything in my Unions.
                # Specifically, if the union has only intrusive queries, and
                # the intersection of every one each of the classes of
                # intrusive query cancels with counterparts in my set of
                # intrusive queries, then the result is an empty query, making
                # this query empty. Furthermore, trying to apply that Union
                # would result in an empty query and errors and headaches. And
                # late nights debugging code.
                if cls == Union and any(d for d in self._in_queries.values()):
                    for q in q_list:
                        all_empty = True
                        for sub_q in q.queries:
                            if not isinstance(sub_q, IntrusiveQueryCore):
                                all_empty = False
                                break
                            compare_ins = [q for d in self._in_queries.values()
                                           for q in d.values()
                                           if q.name == sub_q.name]
                            if not compare_ins:
                                all_empty = False
                                break
                            for in_q in compare_ins:
                                if not (sub_q & in_q).empty:
                                    all_empty = False
                                    break
                            if not all_empty:
                                break
                        empty = all_empty

        # Check to see if the types overlap
        empty |= any(pq.is_inverse_of(self._in_queries['neg'][pn])
                     for pn, pq in self._in_queries['pos'].items()
                     if pn in self._in_queries['neg'])

        super(Intersection, self).__init__(filtered_queries, empty, all_full)

    def __invert__(self):
        new_obj = Union([~q for q in self.queries])
        return new_obj

    @staticmethod
    def _merge(*queries):
        return intersect_all(*queries)

    def _get_table(self, ro):
        if self._mk_hashes_al is not None:
            return self._mk_hashes_al

        if self._injected_queries is not None:
            raise ValueError("Type queries should not be applied to "
                             "Intersection, but handled as an intersected "
                             "query.")

        in_queries = [q for d in self._in_queries.values() for q in d.values()]
        if not in_queries:
            in_queries = None
        queries = [q.get_hash_query(ro, in_queries) for q in self.queries
                   if not q.full and not isinstance(q, IntrusiveQueryCore)]
        if not queries:
            if in_queries:
                queries = [q.get_hash_query(ro) for q in in_queries]
                self._mk_hashes_al = self._merge(*queries).alias(self.name)
            else:
                # There should never be two type queries of the same inversion,
                # they could simply have been merged together.
                raise RuntimeError("Malformed Intersection occurred.")
        elif len(queries) == 1:
            self._mk_hashes_al = queries[0].subquery().alias(self.name)
        else:
            self._mk_hashes_al = self._merge(*queries).alias(self.name)

        return self._mk_hashes_al


class Union(MergeQueryCore):
    """The union of multiple queries.

    Baring special handling, this is generally the result of q1 | q2.

    NOTE: the inverse of a Union is an Intersection (De Morgans's Law)
    """
    name = 'union'
    join_word = 'or'

    def __init__(self, query_list):
        # Break queries into groups to check for inversions, and check to see
        # that not all queries are empty. Special handling is also applied for
        # hash queries.
        other_queries = set()
        query_groups = defaultdict(list)
        mergeable_types = (HasHash, FromPapers, IntrusiveQueryCore)
        merge_grps = defaultdict(lambda: {True: [], False: []})
        full = False
        all_empty = True
        for query in query_list:
            if not query.empty:
                all_empty = False

            if any(isinstance(query, t) for t in mergeable_types):
                merge_grps[query.__class__][query._inverted].append(query)
            else:
                other_queries.add(query)
                query_groups[query.__class__].append(query)

        # Merge up the hash queries.
        for grp in (g for d in merge_grps.values() for g in d.values()):
            if len(grp) == 1:
                other_queries.add(grp[0])
            elif len(grp) > 1:
                query = grp[0]
                for other_query in grp[1:]:
                    query |= other_query
                if query.full:
                    full = True
                other_queries.add(query)

        # Check if any of the resulting queries so far is a logical query of
        # everything.
        full |= any(q.full for q in other_queries)

        # If it isn't already clear that we cover the space, look through all
        # the query groups for inverse pairs, any one of which would mean we
        # contain everything.
        if not full:
            for q_list in query_groups.values():
                if len(q_list) > 1:
                    for q1, q2 in combinations(q_list, 2):
                        if q1.is_inverse_of(q2):
                            full = True

        super(Union, self).__init__(other_queries, all_empty, full)

    def __invert__(self):
        inv_queries = [~q for q in self.queries]

        # If all the queries are SourceCore, this should be passed back to the
        # specialized SourceIntersection.
        if all(isinstance(q, SourceCore) for q in self.queries):
            return SourceIntersection(inv_queries)
        return Intersection(inv_queries)

    @staticmethod
    def _merge(*queries):
        return union_all(*queries)

    def _get_table(self, ro):
        if self._mk_hashes_al is None:
            mk_hashes_q_list = []
            for q in self.queries:
                if q.empty:
                    continue

                # If it is an intrusive query, merge it with the given
                # intrusive queries of the same type, or else pass the type
                # queries along.
                if isinstance(q, IntrusiveQueryCore) \
                        and self._injected_queries:
                    like_queries = []
                    in_queries = []
                    for in_q in self._injected_queries:
                        if in_q.name == q.name:
                            like_queries.append(in_q)
                        else:
                            in_queries.append(in_q)
                else:
                    like_queries = []
                    in_queries = self._injected_queries

                if like_queries:
                    for in_q in like_queries:
                        q &= in_q
                    if q.empty:
                        continue
                if not in_queries:
                    in_queries = None
                mkhq = q.get_hash_query(ro, in_queries)
                mk_hashes_q_list.append(mkhq)

            if len(mk_hashes_q_list) == 1:
                self._mk_hashes_al = (mk_hashes_q_list[0].subquery()
                                                         .alias(self.name))
            else:
                self._mk_hashes_al = (self._merge(*mk_hashes_q_list)
                                          .alias(self.name))
        return self._mk_hashes_al


class _QueryEvidenceFilter:
    def __init__(self, table_name, get_clause):
        self.table_name = table_name
        self.get_clause = get_clause

    def join_table(self, ro, query, tables_joined=None):
        if self.table_name == 'raw_stmt_src':
            ret = query.filter(ro.RawStmtSrc.sid == ro.FastRawPaLink.id)
        elif self.table_name == 'raw_stmt_mesh':
            ret = query.outerjoin(ro.RawStmtMesh,
                                  ro.RawStmtMesh.sid == ro.FastRawPaLink.id)
        elif self.table_name == 'reading_ref_link':
            ret = query.outerjoin(
                ro.ReadingRefLink,
                ro.ReadingRefLink.rid == ro.FastRawPaLink.reading_id
            )
        else:
            raise ValueError(f"No join defined for readonly table "
                             f"'{self.table_name}'")

        if tables_joined is not None:
            tables_joined.add(self.table_name)
        return ret


class EvidenceFilter:
    """Object for handling filtering of evidence.

    We need to be able to perform logical operations between evidence to handle
    important cases:

    HasSource(['reach']) & FromMeshId(['D0001'])
    -> we might reasonably want to filter evidence for the second subquery but
       not the first.

    HasOnlySource(['reach']) & FromMeshId(['D00001'])
    -> Here we would likely want to filter the evidence for both sub queries.

    HasOnlySource(['reach']) | FromMeshId(['D000001'])
    -> Not sure what this even means (its purpose)....not sure what we'd do for
       evidence filtering when the original statements are or'ed

    HasDatabases() & FromMeshId(['D000001'])
    -> Here you COULDN'T perform an & on the evidence, because the two sources
       are mutually exclusive (only readings connect to mesh annotations).
       However it could make sense you would want to do an "or" between the
       evidence, so the evidence is either from a database or from a mesh
       annotated document.

    "filter all the evidence" and "filter none of the evidence" should
    definitely be options. Although "Filter for all" might run into usues with
    the "HasDatabase and FromMeshId" scenario. I think no evidence filter should
    be the default, and if you attempt a bogus "filter all evidence" (as with
    that scenario) you get an error.
    """

    def __init__(self, filters=None, joiner='and'):
        if filters is None:
            filters = []
        self.filters = filters
        self.joiner = joiner

    @classmethod
    def from_filter(cls, table_name, get_clause):
        return cls([_QueryEvidenceFilter(table_name, get_clause)])

    def _merge(self, method, other):
        if not isinstance(other, EvidenceFilter):
            raise ValueError(f"Type {type(other)} cannot use __{method}__ with "
                             f"{self.__class__.__name__}.")
        if self.joiner == method:
            if other.joiner == method or len(other.filters) == 1:
                ret = EvidenceFilter(self.filters + other.filters)
            else:
                ret = EvidenceFilter(self.filters + [other])
        else:
            if other.joiner == method:
                if len(self.filters) == 1:
                    ret = EvidenceFilter(other.filters + self.filters)
                else:
                    ret = EvidenceFilter(other.filters + [self])
            else:
                if len(self.filters) == 1:
                    if len(other.filters) == 1:
                        ret = EvidenceFilter(self.filters + other.filters)
                    else:
                        ret = EvidenceFilter(self.filters + [other])
                else:
                    if len(other.filters) == 1:
                        ret = EvidenceFilter(other.filters + [self])
                    else:
                        ret = EvidenceFilter([self, other])
        return ret

    def __and__(self, other):
        return self._merge('and', other)

    def __or__(self, other):
        return self._merge('or', other)

    def _get_clause_list(self, ro):
        return [f.get_clause(ro) for f in self.filters]

    def get_clause(self, ro):
        if self.joiner == 'and':
            return and_(*self._get_clause_list(ro))
        else:
            return or_(*self._get_clause_list(ro))

    def apply_filter(self, ro, query):
        if self.joiner == 'and':
            return query.filter(*self._get_clause_list(ro))
        else:
            return query.filter(self.get_clause(ro))

    def join_table(self, ro, query, tables_joined=None):
        if tables_joined is None:
            tables_joined = set()

        for ev_filter in self.filters:
            query = ev_filter.join_table(ro, query, tables_joined)

        return query


def _get_raw_texts(stmt_json):
    raw_text = []
    agent_names = get_statement_by_name(stmt_json['type'])._agent_order
    for ag_name in agent_names:
        ag_value = stmt_json.get(ag_name, None)
        if isinstance(ag_value, dict):
            raw_text.append(ag_value['db_refs'].get('TEXT'))
        elif ag_value is None:
            raw_text.append(None)
        else:
            for ag in ag_value:
                raw_text.append(ag['db_refs'].get('TEXT'))
    return raw_text
