'''
This module implements a simple, distributed Key-Value database that uses Paxos
for ensuring consistency between nodes. The goal of this module is to provide a
simple and correct implementation that is useful both as an example for using
zpax for distributed consensus as well as an actual, embedded database for real
applications. Due to the simplicity goal, the performance of this
implementation is not stellar but should be sufficient for lightweight usage.
'''

import os.path
import sqlite3
import json

from zpax import tzmq, node
from paxos import multi, basic

from twisted.internet import defer, task, reactor


_ZPAX_CONFIG_KEY = '__zpax_config__'


tables = dict()

tables['kv'] = '''
key      text PRIMARY KEY,
value    text,
resolution integer
'''


class MissingConfiguration (Exception):
    pass


class SqliteDB (object):

    def __init__(self, fn):
        self._fn = fn
        create = not os.path.exists(fn)
        
        self._con = sqlite3.connect(fn)
        self._cur = self._con.cursor()

        if create:
            self.create_db()

            
    def create_db(self):
        cur = self._con.cursor()

        for k,v in tables.iteritems():
            cur.execute('create table {} ({})'.format(k,v))

        cur.execute('create index resolution_index on kv (resolution)')

        self._con.commit()
        cur.close()


    def get_value(self, key):
        r = self._cur.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
        if r:
            return r[0]

        
    def get_resolution(self, key):
        r = self._cur.execute('SELECT resolution FROM kv WHERE key=?', (key,)).fetchone()
        if r:
            return r[0]

        
    def update_key(self, key, value, resolution_number):
        prevpn = self.get_resolution(key)

        if prevpn is None:
            self._cur.execute('INSERT INTO kv VALUES (?, ?, ?)',
                              (key, value, resolution_number))
            self._con.commit()
            
        elif resolution_number > prevpn:
            self._cur.execute('UPDATE kv SET value=?, resolution=? WHERE key=?',
                              (value, resolution_number, key))
            self._con.commit()

            
    def get_last_resolution(self):
        r = self._cur.execute('SELECT MAX(resolution) FROM kv').fetchone()[0]

        return r if r is not None else -1

    
    def iter_updates(self, start_resolution, end_resolution=2**32):
        c = self._con.cursor()
        c.execute('SELECT key,value,resolution FROM kv WHERE resolution>? AND resolution<?  ORDER BY resolution',
                  (start_resolution, end_resolution))
        return c



class KeyValNode (node.BasicNode):
    '''
    This class implements the Paxos logic for KeyValueDB. It extends
    node.BasicNode in two significant ways. First, it embeds within
    each heartbeat message the current multi-paxos sequence number. This
    allows late-joining/recovering nodes to quickly discover that their
    databases are out of sync and begin the catchup process. Second,
    this node drops all Paxos messages while the database is catching
    up. This prevents newly chosen values from entering the database
    prior to the completion of the catchup process. 
    '''

    chatty = False

    def __init__(self,
                 kvdb,
                 node_uid,
                 durable_dir,
                 object_id):

        super(KeyValNode,self).__init__(node_uid, durable_dir, object_id)
        
        self.kvdb = kvdb


    def getHeartbeatData(self):
        return dict( seq_num = self.getCurrentSequenceNumber() )
    
    
    def onHeartbeat(self, data):
        if data['seq_num'] - 1 > self.kvdb.getMaxDBSequenceNumber():
            if data['seq_num'] > self.getCurrentSequenceNumber():
                self.setCurrentSequenceNumber( data['seq_num'] )

            self.kvdb.catchup()


    # Override the sequence checking function in the baseclass to cause all
    # packets to be dropped from our Paxos implementation while our database
    # is out of sync
    def checkSequence(self, header):
        return super(KeyValNode,self).checkSequence(header) and not self.kvdb.isCatchingUp()


    def onBehindInSequence(self, old_sequence_number, new_sequence_number):
        self.kvdb.catchup()

        
    def onProposalResolution(self, instance_num, value):
        # This method is only called when our database is current
        
        key, value = json.loads(value)

        self.kvdb.onValueSet( key, value, instance_num )
        



class KeyValueDB (node.JSONResponder):
    '''
    This class implements a distributed key=value database that uses Paxos to
    coordinate database updates. Unlike the replacated state machine design
    typically discussed in Paxos literature, this implementation takes a
    simpler approach to ensure consistency. Each key/value update includes in
    the database the Multi-Paxos instance number used to set the value for that
    key.

    Nodes detect that their database is out of sync with their peers when it
    sees a heartbeat message for for a Multi-Paxos instance ahead of what it is
    expecting. When this occurs, the node suspends it's participation in the
    Paxos protocol and synchronizes it's database. This is accomplished by
    continually requesting key-value pairs with instance ids greater than what
    it has already received. These are requested in ascending order until the
    node recieves the key-value pair with an instance number that is 1 less
    than the current Multi-Paxos instance under negotiation. This indicates
    that the node has fully synchronized with it's peers and may rejoin the
    Paxos protocol.

    To support the addition and removal of nodes, the configuration for the
    paxos configuration is, itself, stored in the database. This
    automatically ensures that at least a quorum number of nodes always agree
    on what the current configuration is and, consequently, ensures that
    progress can always be made. Note, however, that if encryption is
    being used and the encryption key changes, some additional work will be
    required to enable the out-of-date nodes to catch up.

    With this implementation, queries to nodes may return data that is out of
    date. "Retrieve the most recent value" is not an operation that this
    implementation can reliably handle; it is imposible to reliably detect
    whether this node's data is consistent with it's peers at any given point
    in time. Successful handling of this operation requires either that the
    read operation flow through the Paxos algorighm itself (and in which case
    the value could be rendered out-of-date even before it is delivered to the
    client) or leadership-leases must be used. Leadership leases are relatively
    straight-forward to implement but are omitted here for the sake of
    simplicity.
    '''

    _node_klass = KeyValNode
    
    def __init__(self, node_uid,
                 database_dir,
                 database_filename=None,
                 catchup_retry_delay=2.0,
                 catchup_num_items=2):

        if database_filename is None:
            database_filename = os.path.join(database_dir, 'db.sqlite')

        if database_filename == ':memory:':
            durable_dir = None
            durable_id  = None
        else:
            durable_dir = database_dir
            durable_id  = os.path.basename(database_filename + '.paxos')

        self.node_uid = node_uid
        self.kv_node = self._node_klass(self, node_uid, durable_dir, durable_id)

        # By default, prevent arbitrary clients from proposing new
        # configuration values
        self.allow_config_proposals = False

        self.catchup_retry_delay = catchup_retry_delay
        self.catchup_num_items   = catchup_num_items
            
        self.db            = SqliteDB( database_filename )
        self.db_seq        = self.db.get_last_resolution()
        self.catching_up   = False
        self.catchup_retry = None

        self.rep_addr      = None
        self.other_reps    = None
            
        self.rep           = None
        self.dlr           = None

        if self.isInitialized():
            self._loadConfiguration()


    def onCaughtUp(self):
        pass

    
    def _loadConfiguration(self, cfg_str=None):
        if cfg_str is None:
            cfg_str = self.db.get_value(_ZPAX_CONFIG_KEY)

        cfg = json.loads(cfg_str)

        zpax_nodes = dict()
        kv_reps    = set()
        my_addr    = None

        for n in cfg['nodes']:
            zpax_nodes[ n['uid'] ] = (n['pax_rep_addr'], n['pax_pub_addr'])
            
            if self.kv_node.node_uid == n['uid']:
                my_addr = n['kv_rep_addr']
            else:
                kv_reps.add( n['kv_rep_addr'] )

        if my_addr is None:
            raise MissingConfiguration('Configuration is missing configuration for this node')

        if self.rep_addr is None or self.rep_addr != my_addr:
            self.rep_addr = my_addr
            if self.rep is not None:
                self.rep.close()
            self.rep = tzmq.ZmqRepSocket()
            self.rep.linger = 0
            self.rep.bind(self.rep_addr)
            self.rep.messageReceived = self._generateResponder( '_REP_' )

        if self.other_reps is None or not self.other_reps == kv_reps:
            self.other_reps = kv_reps
            if self.dlr is not None:
                self.dlr.close()
            self.dlr = tzmq.ZmqDealerSocket()
            self.dlr.messageReceived = self._generateResponder( '_DLR_',
                                                                lambda x : x[1:])

            for x in kv_reps:
                self.dlr.connect(x)
                

        # Quorum size may be specified in the config if tighter consistency
        # requirements are desired
        if 'quorum_size' in cfg:
            quorum_size = cfg['quorum_size']
        else:
            quorum_size = len(cfg['nodes'])/2 + 1

        if not self.kv_node.initialized:
            self.kv_node.initialize( quorum_size )

        elif self.kv_node.quorum_size != quorum_size:
            self.kv_node.changeQuorumSize( quorum_size )

        self.kv_node.connect( zpax_nodes )


    def isInitialized(self):
        return self.db.get_value(_ZPAX_CONFIG_KEY) is not None

    
    def initialize(self, config_str):
        if self.isInitialized():
            raise Exception('Node already initialized')
        
        self.db.update_key(_ZPAX_CONFIG_KEY, config_str, -1)
        self._loadConfiguration()

                
    def shutdown(self):
        if self.dlr:
            self.dlr.close()
        self.rep.close()
        if self.catchup_retry and self.catchup_retry.active():
            self.catchup_retry.cancel()
        self.kv_node.shutdown()

        
    def getMaxDBSequenceNumber(self):
        return self.db_seq

    
    def isCatchingUp(self):
        return self.catching_up


    def onValueSet(self, key, value, instance_num):
        if key == _ZPAX_CONFIG_KEY:
            try:
                self._loadConfiguration( value )
            except MissingConfiguration:
                pass # We've been removed from the inner circle :(
        self.db.update_key( key, value, instance_num )        
        self.db_seq = instance_num


    def catchup(self):
        if self.catching_up or self.db_seq == self.kv_node.getCurrentSequenceNumber() - 1:
            return 
        
        self._catchup()

        
    def _catchup(self):
        self.catching_up = self.db_seq != self.kv_node.getCurrentSequenceNumber() - 1
        
        if not self.catching_up:
            self.onCaughtUp()
            return

        self.catchup_retry = reactor.callLater(self.catchup_retry_delay,
                                               self._catchup)

        self.dlr.send( '', json.dumps( dict(type='catchup_request', last_known_seq=self.db_seq) ) )
        

        
    #--------------------------------------------------------------------------
    # REQ Socket
    #
    def _DLR_catchup_data(self, msg):
        if self.db_seq != msg['from_seq']:
            # Reply to an old request. Ignore it.
            return
            
        if self.catchup_retry and self.catchup_retry.active():
            self.catchup_retry.cancel()
            self.catchup_retry = None
            
        if msg['from_seq'] == self.db_seq:
            for key, val, seq_num in msg['key_val_seq_list']:
                if key == _ZPAX_CONFIG_KEY:
                    try:
                        self._loadConfiguration(val)
                    except MissingConfiguration:
                        # We've been removed from the inner circle :(
                        pass
                self.db.update_key(key, val, seq_num)
                
            self.db_seq = self.db.get_last_resolution()

        self._catchup()

    #--------------------------------------------------------------------------
    # REP Socket
    #           
    def rep_reply(self, **kwargs):
        self.rep.send( json.dumps(kwargs) )

        
    def _REP_propose_value(self, header):
        try:
            if not self.allow_config_proposals and header['key'] == _ZPAX_CONFIG_KEY:
                raise Exception('Access Denied')
            jstr = json.dumps( [header['key'], header['value']] )
            self.kv_node.proposeValue(jstr)
            self.rep_reply( proposed = True )
        except node.ProposalFailed, e:
            self.rep_reply(proposed=False, message=str(e))

            
    def _REP_query_value(self, header):
        if not self.allow_config_proposals and header['key'] == _ZPAX_CONFIG_KEY:
            self.rep_reply(error='Access Denied')
        else:
            self.rep_reply( value = self.db.get_value(header['key']) )


    def _REP_catchup_request(self, header):
        l = list()
        
        for tpl in self.db.iter_updates(header['last_known_seq']):
            l.append( tpl )
            if len(l) == self.catchup_num_items:
                break

        self.rep_reply( type             = 'catchup_data',
                        from_seq         = header['last_known_seq'],
                        key_val_seq_list = l )

