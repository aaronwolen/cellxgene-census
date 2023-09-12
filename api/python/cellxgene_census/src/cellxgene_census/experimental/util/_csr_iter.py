from typing import Generator, Iterator, Tuple

import numpy as np
import numpy.typing as npt
import pandas as pd
import pyarrow as pa
import scipy.sparse as sparse
import tiledbsoma as soma
from typing_extensions import Literal

from ._eager_iter import _EagerIterator

_RT = Tuple[Tuple[npt.NDArray[np.int64], npt.NDArray[np.int64]], sparse.spmatrix]


def X_sparse_iter(
    query: soma.ExperimentAxisQuery,
    X_name: str = "raw",
    axis: int = 0,
    stride: int = 2**16,
    fmt: Literal["csr", "csc"] = "csr",
    use_eager_fetch: bool = True,
    reindex_obs: bool = True,
    reindex_var: bool = True,
) -> Iterator[_RT]:
    """
    Iterate over rows (or columns) of the query results X matrix, with pagination to
    control peak memory usage for large result sets. Each iteration step yields:
        * obs_coords (coordinates)
        * var_coords (coordinates)
        * a page of X contents as a SciPy csr_matrix or csc_matrix

    If reindex_obs (default), the rows of each X page are indexed positionally to
    obs_coords, i.e. the original soma_joinid (soma_dim_0) of page row i is given by
    obs_coords[i]. If not reindex_obs, the X page rows are indexed by soma_dim_0
    directly.

    Similarly, the columns of each X page may be indexed positionally by var_coords
    (default) or by soma_dim_1 directly (reindex_var=False).

    Reindexing an axis tends to streamline downstream processing except when:
    1. the query returns nearly all rows/columns on that axis, *or*
    2. the application needs to reindex it in some other way, regardless.

    Args:
        query:
            A SOMA ExperimentAxisQuery defining the coordinates over which the iterator will
            read.
        X_name:
            The name of the X layer.
        axis:
            The axis to iterate over, where zero (0) is obs axis and one (1)
            is the var axis. Currently only axis 0 (obs axis) is supported.
        stride:
            The page size to return in each step (number of obs rows or var columns).
        fmt:
            The SciPy sparse array layout. Supported: 'csc' and 'csr' (default).
        use_eager_fetch:
            If true, will use multiple threads to parallelize reading
            and processing. This will improve speed, but at the cost
            of some additional memory use.
        reindex_obs:
            (see above)
        reindex_var:
            (see above)

    Returns:
        An iterator which iterates over a tuple of:
            (obs_coords, var_coords)
            SciPy sparse matrix

    Examples:
        >>> with cellxgene_census.open_soma() as census:
        ...     exp = census["census_data"][experiment]
        ...     with exp.axis_query(measurement_name="RNA") as query:
        ...         for (obs_soma_joinids, var_soma_joinids), X_chunk in X_sparse_iter(
        ...             query, X_name="raw", stride=1000
        ...         ):
        ...             # X_chunk is a scipy.csr_matrix of csc_matrix
        ...             # For each X_chunk[i, j], the associated soma_joinid is
        ...             # obs_soma_joinids[i] and var_soma_joinids[j]
        ...             ...

    Lifecycle:
        experimental

    See also: https://github.com/single-cell-data/TileDB-SOMA/issues/1528
    """
    if fmt == "csr":
        fmt_ctor = sparse.csr_matrix
    elif fmt == "csc":
        fmt_ctor = sparse.csc_matrix
    else:
        raise ValueError("fmt must be 'csr' or 'csc'")

    if axis != 0:
        raise ValueError("axis must be zero (obs)")

    # Lazy partition array by chunk_size on first dimension
    obs_coords = query.obs_joinids().to_numpy()
    obs_coord_chunker = (obs_coords[i : i + stride] for i in range(0, len(obs_coords), stride))

    X = query._ms.X[X_name]
    var_coords = query.var_joinids().to_numpy()

    # Lazy read of X chunk. Yields ((obs_coords, var_coords), (soma_data, soma_dim_0, soma_dim_1))
    def soma_tbl_vectors(tbl: pa.Table) -> Tuple[npt.NDArray[np.float64], npt.NDArray[np.int64], npt.NDArray[np.int64]]:
        return tuple(tbl[col].to_numpy() for col in ("soma_data", "soma_dim_0", "soma_dim_1"))  # type: ignore

    table_reader = (
        (
            (obs_coords_chunk, var_coords),
            soma_tbl_vectors(X.read(coords=(obs_coords_chunk, var_coords)).tables().concat()),
        )
        for obs_coords_chunk in obs_coord_chunker
    )
    if use_eager_fetch:
        table_reader = (t for t in _EagerIterator(table_reader, query._threadpool))

    coo_reindexer = table_reader
    if reindex_obs or reindex_var:
        # lazy reindexing of soma_dim_0 and soma_dim_1 to obs_coords and var_coords positions
        coo_reindexer = (
            (
                (obs_coords_chunk, var_coords),
                (
                    soma_data,
                    pd.Index(obs_coords_chunk).get_indexer(soma_dim_0) if reindex_obs else soma_dim_0,
                    query.indexer.by_var(soma_dim_1) if reindex_var else soma_dim_1,
                ),
            )
            for (obs_coords_chunk, var_coords), (soma_data, soma_dim_0, soma_dim_1) in table_reader
        )
        if use_eager_fetch:
            coo_reindexer = (t for t in _EagerIterator(coo_reindexer, query._threadpool))

    # lazy convert to Scipy sparse.csr_matrix
    csr_reader: Generator[_RT, None, None] = (
        (
            (obs_coords_chunk, var_coords),
            fmt_ctor(
                sparse.coo_matrix(
                    (data, (i, j)),
                    shape=(
                        (len(obs_coords_chunk) if reindex_obs else X.shape[0]),
                        (query.n_vars if reindex_var else X.shape[1]),
                    ),
                )
            ),
        )
        for (obs_coords_chunk, var_coords), (data, i, j) in coo_reindexer
    )
    if use_eager_fetch:
        csr_reader = (t for t in _EagerIterator(csr_reader, query._threadpool))

    yield from csr_reader
