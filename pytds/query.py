from datetime import datetime
from tds_checks import *
from tds import *
from util import *
from tdsproto import *
from net import *
from mem import _Column
from data import *

def START_QUERY(tds):
    if IS_TDS72_PLUS(tds):
        tds_start_query(tds)

tds72_query_start = str(bytearray([
    #/* total length */
    0x16, 0, 0, 0,
    #/* length */
    0x12, 0, 0, 0,
    #/* type */
    0x02, 0,
    #/* transaction */
    0, 0, 0, 0, 0, 0, 0, 0,
    #/* request count */
    1, 0, 0, 0]))

def tds_start_query(tds):
    w = tds._writer
    w.write(tds72_query_start[:10])
    assert len(tds.tds72_transaction) == 8
    w.write(tds.tds72_transaction)
    assert len(tds72_query_start[10 + 8:]) == 4
    w.write(tds72_query_start[10 + 8:])

def tds_query_flush_packet(tds):
    # TODO depend on result ??
    tds_set_state(tds, TDS_PENDING)
    tds._writer.flush()

def convert_params(tds, parameters):
    if isinstance(parameters, dict):
        return [make_param(tds, name, value) for name, value in parameters.items()]
    else:
        params = []
        for parameter in parameters:
            if type(parameter) is output:
                raise Exception('not implemented')
                #param_type = parameter.type
                #param_value = parameter.value
                #param_output = True
            elif isinstance(parameter, _Column):
                params.append(parameter)
            else:
                params.append(make_param(tds, '', parameter))
        return params

def make_param(tds, name, value):
    column = _Column()
    column.column_name = name
    column.flags = 0
    if isinstance(value, output):
        column.flags |= fByRefValue
        value = value.value
    if value is default:
        column.flags = fDefaultValue
        col_type = XSYBVARCHAR
        size = 1
        column.column_varint_size = tds_get_varint_size(tds, col_type)
        value = None
    elif value is None:
        col_type = XSYBVARCHAR
        size = 1
        column.column_varint_size = tds_get_varint_size(tds, col_type)
    elif isinstance(value, int):
        if -2**31 <= value <= 2**31 -1:
            col_type = SYBINTN
            size = 4
        else:
            col_type = SYBINT8
            size = 8
        column.column_varint_size = tds_get_varint_size(tds, col_type)
    elif isinstance(value, float):
        col_type = SYBFLTN
        size = 8
        column.column_varint_size = tds_get_varint_size(tds, col_type)
    elif isinstance(value, Binary):
        if len(value) > 8000:
            if IS_TDS72_PLUS(tds):
                col_type = XSYBVARBINARY
                column.column_varint_size = 8 # nvarchar(max)
            else:
                col_type = SYBIMAGE
                column.column_varint_size = tds_get_varint_size(tds, col_type)
        else:
            col_type = XSYBVARBINARY
            column.column_varint_size = tds_get_varint_size(tds, col_type)
        size = len(value)
    elif isinstance(value, (str, unicode)):
        if len(value) > 4000:
            if IS_TDS72_PLUS(tds):
                col_type = XSYBNVARCHAR
                column.column_varint_size = 8 # nvarchar(max)
            else:
                col_type = SYBNTEXT
                column.column_varint_size = tds_get_varint_size(tds, col_type)
        else:
            col_type = XSYBNVARCHAR
            column.column_varint_size = tds_get_varint_size(tds, col_type)
        size = len(value) * 2
        column.char_conv = tds.char_convs[client2ucs2]
    elif isinstance(value, datetime):
        col_type = SYBDATETIMN
        size = 8
        column.column_varint_size = tds_get_varint_size(tds, col_type)
    elif isinstance(value, Decimal):
        col_type = SYBDECIMAL
        _, digits, exp = value.as_tuple()
        size = 12
        column.column_scale = -exp
        column.column_prec = max(len(digits), column.column_scale)
        column.column_varint_size = tds_get_varint_size(tds, col_type)
    else:
        raise Exception('NotSupportedError: Unable to determine database type')
    column.on_server.column_type = col_type
    column.column_size = column.on_server.column_size = size
    column.value = value
    column.funcs = tds_get_column_funcs(tds, col_type)
    return column

def _submit_rpc(tds, rpc_name, params, flags):
    tds.cur_dyn = None
    w = tds._writer
    if IS_TDS7_PLUS(tds):
        w.begin_packet(TDS_RPC)
        START_QUERY(tds)
        if IS_TDS71_PLUS(tds) and isinstance(rpc_name, InternalProc):
            w.put_smallint(-1)
            w.put_smallint(rpc_name.proc_id)
        else:
            w.put_smallint(len(rpc_name))
            w.write_ucs2(rpc_name)
        #
        # TODO support flags
        # bit 0 (1 as flag) in TDS7/TDS5 is "recompile"
        # bit 1 (2 as flag) in TDS7+ is "no metadata" bit this will prevent sending of column infos
        #
        w.put_usmallint(flags)
        params = convert_params(tds, params)
        for param in params:
            tds_put_data_info(tds, param)
            param.funcs.put_data(tds, param)
        #tds_query_flush_packet(tds)
    elif IS_TDS5_PLUS(tds):
        w.begin_packet(TDS_NORMAL)
        w.put_byte(TDS_DBRPC_TOKEN)
        # TODO ICONV convert rpc name
        w.put_smallint(len(rpc_name) + 3)
        w.put_byte(len(rpc_name))
        w.write(rpc_name)
        # TODO flags
        w.put_smallint(2 if params else 0)

        if params:
            tds_put_params(tds, params, TDS_PUT_DATA_USE_NAME)

        # send it
        #tds_query_flush_packet(tds)
    else:
        # emulate it for TDS4.x, send RPC for mssql
        return tds_send_emulated_rpc(tds, rpc_name, params)

def tds_submit_rpc(tds, rpc_name, params=(), flags=0):
    if tds_set_state(tds, TDS_QUERYING) != TDS_QUERYING:
        raise Exception('TDS_FAIL')
    try:
        _submit_rpc(tds, rpc_name, params, flags)
        tds_query_flush_packet(tds)
    except:
        tds_set_state(tds, TDS_IDLE)
        raise

#
# tds_submit_query() sends a language string to the database server for
# processing.  TDS 4.2 is a plain text message with a packet type of 0x01,
# TDS 7.0 is a unicode string with packet type 0x01, and TDS 5.0 uses a
# TDS_LANGUAGE_TOKEN to encapsulate the query and a packet type of 0x0f.
# \param tds state information for the socket and the TDS protocol
# \param query  language query to submit
# \param params parameters of query
# \return TDS_FAIL or TDS_SUCCESS
#
def tds_submit_query(tds, query, params=(), flags=0):
    logger.info('tds_submit_query(%s, %s)', query, params)
    #size_t query_len;
    CHECK_TDS_EXTRA(tds)
    if params:
        CHECK_PARAMINFO_EXTRA(params)

    if not query:
        raise Exception('TDS_FAIL')

    if tds_set_state(tds, TDS_QUERYING) != TDS_QUERYING:
        raise Exception('TDS_FAIL')
    try:
        tds.res_info = None
        w = tds._writer
        if IS_TDS50(tds):
            new_query = None
            # are there '?' style parameters ?
            if tds_next_placeholder(query):
                new_query = tds5_fix_dot_query(query, params)
                query = new_query

            w.begin_packet(TDS_NORMAL)
            w.put_byte(TDS_LANGUAGE_TOKEN)
            # TODO ICONV use converted size, not input size and convert string
            w.put_int(len(query) + 1)
            w.put_byte(1 if params else 0) # 1 if there are params, 0 otherwise
            w.write(tds, query)
            if params:
                # add on parameters
                tds_put_params(tds, params, TDS_PUT_DATA_USE_NAME if params.columns[0].column_name else 0)
        elif not IS_TDS7_PLUS(tds) or not params:
            w.begin_packet(TDS_QUERY)
            START_QUERY(tds)
            w.write_ucs2(query)
        else:
            params = convert_params(tds, params)
            param_definition = ','.join('{0} {1}'.format(\
                    p.column_name, tds_get_column_declaration(tds, p))
                for p in params)
            _submit_rpc(tds, SP_EXECUTESQL,\
                    [query, param_definition] + params, 0)
            tds.internal_sp_called = TDS_SP_EXECUTESQL
        tds_query_flush_packet(tds)
    except:
        tds_set_state(tds, TDS_IDLE)
        raise


#/**
# * tds_send_cancel() sends an empty packet (8 byte header only)
# * tds_process_cancel should be called directly after this.
# * \param tds state information for the socket and the TDS protocol
# * \remarks
# *	tcp will either deliver the packet or time out. 
# *	(TIME_WAIT determines how long it waits between retries.)  
# *	
# *	On sending the cancel, we may get EAGAIN.  We then select(2) until we know
# *	either 1) it succeeded or 2) it didn't.  On failure, close the socket,
# *	tell the app, and fail the function.  
# *	
# *	On success, we read(2) and wait for a reply with select(2).  If we get
# *	one, great.  If the client's timeout expires, we tell him, but all we can
# *	do is wait some more or give up and close the connection.  If he tells us
# *	to cancel again, we wait some more.  
# */
def tds_send_cancel(tds):
    if TDS_MUTEX_TRYLOCK(tds.wire_mtx):
        # TODO check
        # signal other socket
        raise Exception('not implemented')
        #tds_conn(tds).s_signal.send((void*) &tds, sizeof(tds))
        return TDS_SUCCESS

    CHECK_TDS_EXTRA(tds);

    logger.debug("tds_send_cancel: %sin_cancel and %sidle".format(
                            ('' if tds.in_cancel else "not "), ('' if tds.state == TDS_IDLE else "not ")))

    # one cancel is sufficient
    if tds.in_cancel or tds.state == TDS_IDLE:
        TDS_MUTEX_UNLOCK(tds.wire_mtx)
        return TDS_SUCCESS

    tds.res_info = None
    rc = tds_put_cancel(tds)
    TDS_MUTEX_UNLOCK(tds.wire_mtx)

    return rc

#
# Put data information to wire
# \param tds    state information for the socket and the TDS protocol
# \param curcol column where to store information
# \param flags  bit flags on how to send data (use TDS_PUT_DATA_USE_NAME for use name information)
# \return TDS_SUCCESS or TDS_FAIL
#
def tds_put_data_info(tds, curcol):
    logger.debug("tds_put_data_info putting param_name")
    w = tds._writer
    if IS_TDS7_PLUS(tds):
        w.put_byte(len(curcol.column_name))
        w.write_ucs2(curcol.column_name)
    else:
        # TODO ICONV convert
        w.put_byte(len(curcol.column_name))
        w.write(curcol.column_name)
    #
    # TODO support other flags (use defaul null/no metadata)
    # bit 1 (2 as flag) in TDS7+ is "default value" bit 
    # (what's the meaning of "default value" ?)
    #

    logger.debug("tds_put_data_info putting status")
    w.put_byte(curcol.flags)
    if not IS_TDS7_PLUS(tds):
        w.put_int(curcol.column_usertype) # usertype
    # FIXME: column_type is wider than one byte.  Do something sensible, not just lop off the high byte.
    w.put_byte(curcol.on_server.column_type)

    curcol.funcs.put_info(tds, curcol)

    # TODO needed in TDS4.2 ?? now is called only is TDS >= 5
    if not IS_TDS7_PLUS(tds):
        w.put_byte(0) # locale info length

#
# Return declaration for column (like "varchar(20)")
# \param tds    state information for the socket and the TDS protocol
# \param curcol column
# \param out    buffer to hold declaration
# \return TDS_FAIL or TDS_SUCCESS
#
def tds_get_column_declaration(tds, curcol):
    max_len = 8000 if IS_TDS7_PLUS(tds) else 255

    size = tds_fix_column_size(tds, curcol)
    t = curcol.on_server.column_type #tds_get_conversion_type(curcol.on_server.column_type, curcol.on_server.column_size)

    if t in (XSYBCHAR, SYBCHAR):
        return "CHAR(%d)" % min(size, max_len)
    elif t in (SYBVARCHAR, XSYBVARCHAR):
        if curcol.column_varint_size == 8:
            return "VARCHAR(MAX)"
        else:
            return "VARCHAR(%d)" % min(size, max_len)
    elif t == SYBINT1:
        return "TINYINT"
    elif t == SYBINT2:
        return "SMALLINT"
    elif t == SYBINT4 or t == SYBINTN and size == 4:
        return "INT"
    elif t == SYBINT8:
        # TODO even for Sybase ??
        return "BIGINT"
    elif t == SYBFLT8 or t == SYBFLTN and size == 8:
        return "FLOAT"
    elif t == SYBDATETIME or t == SYBDATETIMN and size == 8:
        return "DATETIME"
    elif t == SYBBIT:
        return "BIT"
    elif t == SYBTEXT:
        return "TEXT"
    elif t == (SYBLONGBINARY, # TODO correct ??
            SYBIMAGE):
        return "IMAGE"
    elif t == SYBMONEY4:
        return "SMALLMONEY"
    elif t == SYBMONEY:
        return "MONEY"
    elif t == SYBDATETIME4 or t == SYBDATETIMN and size == 4:
        return "SMALLDATETIME"
    elif t == SYBREAL:
        return "REAL"
    elif t in (SYBBINARY, XSYBBINARY):
        return "BINARY(%d)" % min(size, max_len)
    elif t in (SYBVARBINARY, XSYBVARBINARY):
        if curcol.column_varint_size == 8:
            return "VARBINARY(MAX)"
        else:
            return "VARBINARY(%u)" % min(size, max_len)
    elif t == SYBNUMERIC:
        return "NUMERIC(%d,%d)" % (curcol.column_prec, curcol.column_scale)
    elif t == SYBDECIMAL:
        return "DECIMAL(%d,%d)" % (curcol.column_prec, curcol.column_scale)
    elif t == SYBUNIQUE:
        if IS_TDS7_PLUS(tds):
            return "UNIQUEIDENTIFIER"
    elif t == SYBNTEXT:
        if IS_TDS7_PLUS(tds):
            return "NTEXT"
    elif t in (SYBNVARCHAR, XSYBNVARCHAR):
        if curcol.column_varint_size == 8:
            return "NVARCHAR(MAX)"
        elif IS_TDS7_PLUS(tds):
            return "NVARCHAR(%u)" % min(size/2, 4000)
    elif t == XSYBNCHAR:
        if IS_TDS7_PLUS(tds):
            return "NCHAR(%u)" % min(size/2, 4000)
    elif t == SYBVARIANT:
        if IS_TDS7_PLUS(tds):
            return "SQL_VARIANT"
    # TODO support scale !!
    elif t == SYBMSTIME:
        return "TIME"
    elif t == SYBMSDATE:
        return "DATE"
    elif t == SYBMSDATETIME2:
        return "DATETIME2"
    elif t == SYBMSDATETIMEOFFSET:
        return "DATETIMEOFFSET"
    # nullable types should not occur here...
    elif t in (SYBMONEYN, SYBDATETIMN, SYBBITN):
        assert False
        # TODO...
    else:
        raise Exception("Unknown type %d", t)

def tds_submit_begin_tran(tds):
    logger.debug('tds_submit_begin_tran()')
    if IS_TDS72(tds):
        if tds_set_state(tds, TDS_QUERYING) != TDS_QUERYING:
            raise Exception('TDS_FAIL')

        w = tds._writer
        w.begin_packet(TDS7_TRANS)
        tds_start_query(tds)

        # begin transaction
        w.put_smallint(5)
        w.put_byte(0) # new transaction level TODO
        w.put_byte(0) # new transaction name

        tds_query_flush_packet(tds)
    else:
        tds_submit_query(tds, "BEGIN TRANSACTION")

def tds_submit_rollback(tds, cont):
    logger.debug('tds_submit_rollback(%s, %s)', id(tds), cont)
    if IS_TDS72(tds):
        if tds_set_state(tds, TDS_QUERYING) != TDS_QUERYING:
            raise Exception('TDS_FAIL')

        w = tds._writer
        w.begin_packet(TDS7_TRANS)
        tds_start_query(tds)
        w.put_smallint(8) # rollback
        w.put_byte(0) # name
        if cont:
            w.put_byte(1)
            w.put_byte(0) # new transaction level TODO
            w.put_byte(0) # new transaction name
        else:
            w.put_byte(0) # do not continue
        tds_query_flush_packet(tds);
    else:
        tds_submit_query(tds, "IF @@TRANCOUNT > 0 ROLLBACK BEGIN TRANSACTION" if cont else "IF @@TRANCOUNT > 0 ROLLBACK")

def tds_submit_commit(tds, cont):
    logger.debug('tds_submit_commit(%s)', cont)
    if IS_TDS72(tds):
        if tds_set_state(tds, TDS_QUERYING) != TDS_QUERYING:
            raise Exception('TDS_FAIL')

        w = tds._writer
        w.begin_packet(TDS7_TRANS)
        tds_start_query(tds)
        w.put_smallint(7) # commit
        w.put_byte(0) # name
        if cont:
            w.put_byte(1)
            w.put_byte(0) # new transaction level TODO
            w.put_byte(0) # new transaction name
        else:
            w.put_byte(0) # do not continue
        tds_query_flush_packet(tds)
    else:
        tds_submit_query(tds, "IF @@TRANCOUNT > 0 COMMIT BEGIN TRANSACTION" if cont else "IF @@TRANCOUNT > 0 COMMIT")
