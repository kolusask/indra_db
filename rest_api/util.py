import logging
from io import StringIO
from datetime import datetime

logger = logging.getLogger('db rest api - util')


class DbAPIError(Exception):
    pass


def get_s3_client():
    import boto3
    from botocore import config
    return boto3.client('s3', boto3.session.Session().region_name,
                        config=config.Config(s3={'addressing_style': 'path'}))

# ==============================================
# Define some utilities used to resolve queries.
# ==============================================


def process_agent(agent_param):
    """Get the agent id and namespace from an input param."""

    if not agent_param.endswith('@TEXT'):
        param_parts = agent_param.split('@')
        if len(param_parts) == 2:
            ag, ns = param_parts
        elif len(param_parts) == 1:
            ns = 'NAME'
            ag = param_parts[0]
        else:
            raise DbAPIError('Unrecognized agent spec: \"%s\"' % agent_param)
    else:
        ag = agent_param[:-5]
        ns = 'TEXT'

    if ns == 'HGNC-SYMBOL':
        ns = 'NAME'

    logger.info("Resolved %s to ag=%s, ns=%s" % (agent_param, ag, ns))
    if ns == 'AUTO':
        res = gilda_ground(ag)
        ns = res[0]['term']['db']
        ag = res[0]['term']['id']
        logger.info("Auto-mapped grounding with gilda to ag=%s, ns=%s with "
                    "score=%s out of %d options"
                    % (ag, ns, res[0]['score'], len(res)))

    return ag, ns


def gilda_ground(agent_text):
    import requests
    res = requests.post('http://grounding.indra.bio/ground',
                        json={'text': agent_text})
    return res.json()


def get_source(ev_json):
    notes = ev_json.get('annotations')
    if notes is None:
        return
    src = notes.get('content_source')
    if src is None:
        return
    return src.lower()


def sec_since(t):
    return (datetime.now() - t).total_seconds()


class LogTracker(object):
    log_path = '.rest_api_tracker.log'

    def __init__(self):
        root_logger = logging.getLogger()
        self.stream = StringIO()
        sh = logging.StreamHandler(self.stream)
        formatter = logging.Formatter('%(levelname)s: %(name)s %(message)s')
        sh.setFormatter(formatter)
        sh.setLevel(logging.WARNING)
        root_logger.addHandler(sh)
        self.root_logger = root_logger
        return

    def get_messages(self):
        conts = self.stream.getvalue()
        print(conts)
        ret = conts.splitlines()
        return ret

    def get_level_stats(self):
        msg_list = self.get_messages()
        ret = {}
        for msg in msg_list:
            level = msg.split(':')[0]
            if level not in ret.keys():
                ret[level] = 0
            ret[level] += 1
        return ret
