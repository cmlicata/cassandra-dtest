import time
import uuid

from cassandra import InvalidRequest
from cassandra.query import SimpleStatement, dict_factory
from dtest import Tester

from datahelp import create_rows, parse_data_into_dicts, flatten_into_set


class Page(object):
    data = None

    def __init__(self):
        self.data = []

    def add_row(self, row):
        self.data.append(row)


class PageFetcher(object):
    """
    Requests pages, handles their receipt,
    and provides paged data for testing.

    The first page is automatically retrieved, so an initial
    call to request_page is actually getting the *second* page!
    """
    pages = None
    error = None
    future = None
    requested_pages = None
    retrieved_pages = None

    def __init__(self, future):
        self.pages = []

        self.future = future
        self.future.add_callbacks(
            callback=self.handle_page,
            errback=self.handle_error
        )
        # the first page is automagically returned (eventually)
        # so we'll count this as a request, but the retrieved count
        # won't be incremented until it actually arrives
        self.requested_pages = 1
        self.retrieved_pages = 0

        # wait for the first page to arrive, otherwise we may call
        # future.has_more_pages too early, since it should only be
        # called after the first page is returned
        self.wait(seconds=30)

    def handle_page(self, rows):
        self.retrieved_pages += 1
        page = Page()
        self.pages.append(page)

        for row in rows:
            page.add_row(row)

    def handle_error(self, exc):
        self.error = exc
        raise exc

    def request_all_pages(self):
        """
        Requests any remaining pages.

        If the future is exhausted, this is a no-op.
        """
        while self.future.has_more_pages:
            self.future.start_fetching_next_page()
            self.requested_pages += 1
            self.wait()

        return self

    def request_page(self):
        """
        Requests the next page if there is one.

        If the future is exhausted, this is a no-op.
        """
        if self.future.has_more_pages:
            self.future.start_fetching_next_page()
            self.requested_pages += 1
            self.wait()

        return self

    def wait(self, seconds=5):
        """
        Blocks until all *requested* pages have been returned.

        Requests are made by calling request_page and/or request_all_pages.

        Raises RuntimeError if seconds is exceeded.
        """
        expiry = time.time() + seconds

        while time.time() < expiry:
            if self.requested_pages == self.retrieved_pages:
                return self

        raise RuntimeError("Requested pages were not delivered before timeout.")

    def retrieved_pagecount(self):
        """
        Returns count of *retrieved* pages.

        Pages are retrieved by requesting them with request_page and/or request_all_pages.
        """
        return len(self.pages)

    def num_results(self, page_num):
        """
        Returns the number of results found at page_num
        """
        return len(self.pages[page_num - 1].data)

    def num_results_all_pages(self):
        return [len(page.data) for page in self.pages]

    def retrieved_page_data(self, page_num):
        """
        Returns retreived data found at pagenum.

        The page should have already been requested with request_page and/or request_all_pages.
        """
        return self.pages[page_num - 1].data

    def all_retrieved_data(self):
        """
        Returns all retrieved data flattened into a single list (instead of separated into Page objects).

        The page(s) should have already been requested with request_page and/or request_all_pages.
        """
        all_pages_combined = []
        for page in self.pages:
            all_pages_combined.extend(page.data[:])

        return all_pages_combined

    @property  # make property to match python driver api
    def has_more_pages(self):
        """
        Returns bool indicating if there are any pages not retrieved.
        """
        return self.future.has_more_pages


class PageAssertionMixin(object):
    """Can be added to subclasses of unittest.Tester"""
    def assertEqualIgnoreOrder(self, actual, expected):
        return self.assertItemsEqual(actual, expected)

    def assertIsSubsetOf(self, subset, superset):
        assert flatten_into_set(subset).issubset(flatten_into_set(superset))


class BasePagingTester(Tester):
    def prepare(self):
        cluster = self.cluster
        cluster.populate(3).start()
        node1, node2, node3 = cluster.nodelist()
        cursor = self.cql_connection(node1)
        cursor.row_factory = dict_factory
        return cursor


class TestPagingSize(BasePagingTester, PageAssertionMixin):
    """
    Basic tests relating to page size (relative to results set)
    and validation of page size setting.
    """
    def test_with_no_results(self):
        """
        No errors when a page is requested and query has no results.
        """
        cursor = self.prepare()
        self.create_ks(cursor, 'test_paging_size', 2)
        cursor.execute("CREATE TABLE paging_test ( id int PRIMARY KEY, value text )")

        # run a query that has no results and make sure it's exhausted
        future = cursor.execute_async(
            SimpleStatement("select * from paging_test", fetch_size=100)
        )

        pf = PageFetcher(future)
        pf.request_all_pages()
        self.assertEqual([], pf.all_retrieved_data())
        self.assertFalse(pf.has_more_pages)

    def test_with_less_results_than_page_size(self):
        cursor = self.prepare()
        self.create_ks(cursor, 'test_paging_size', 2)
        cursor.execute("CREATE TABLE paging_test ( id int PRIMARY KEY, value text )")

        data = """
            |id| value          |
            |1 |testing         |
            |2 |and more testing|
            |3 |and more testing|
            |4 |and more testing|
            |5 |and more testing|
            """
        expected_data = create_rows(data, cursor, 'paging_test', format_funcs={'id': int, 'value': unicode})

        future = cursor.execute_async(
            SimpleStatement("select * from paging_test", fetch_size=100)
        )
        pf = PageFetcher(future)
        pf.request_all_pages()

        self.assertFalse(pf.has_more_pages)
        self.assertEqual(len(expected_data), len(pf.all_retrieved_data()))

    def test_with_more_results_than_page_size(self):
        cursor = self.prepare()
        self.create_ks(cursor, 'test_paging_size', 2)
        cursor.execute("CREATE TABLE paging_test ( id int PRIMARY KEY, value text )")

        data = """
            |id| value          |
            |1 |testing         |
            |2 |and more testing|
            |3 |and more testing|
            |4 |and more testing|
            |5 |and more testing|
            |6 |testing         |
            |7 |and more testing|
            |8 |and more testing|
            |9 |and more testing|
            """
        expected_data = create_rows(data, cursor, 'paging_test', format_funcs={'id': int, 'value': unicode})

        future = cursor.execute_async(
            SimpleStatement("select * from paging_test", fetch_size=5)
        )

        pf = PageFetcher(future).request_all_pages()

        self.assertEqual(pf.retrieved_pagecount(), 2)
        self.assertEqual(pf.num_results_all_pages(), [5, 4])

        # make sure expected and actual have same data elements (ignoring order)
        self.assertEqualIgnoreOrder(pf.all_retrieved_data(), expected_data)

    def test_with_equal_results_to_page_size(self):
        cursor = self.prepare()
        self.create_ks(cursor, 'test_paging_size', 2)
        cursor.execute("CREATE TABLE paging_test ( id int PRIMARY KEY, value text )")

        data = """
            |id| value          |
            |1 |testing         |
            |2 |and more testing|
            |3 |and more testing|
            |4 |and more testing|
            |5 |and more testing|
            """
        expected_data = create_rows(data, cursor, 'paging_test', format_funcs={'id': int, 'value': unicode})

        future = cursor.execute_async(
            SimpleStatement("select * from paging_test", fetch_size=5)
        )

        pf = PageFetcher(future).request_all_pages()

        self.assertEqual(pf.num_results_all_pages(), [5])
        self.assertEqual(pf.retrieved_pagecount(), 1)

        # make sure expected and actual have same data elements (ignoring order)
        self.assertEqualIgnoreOrder(pf.all_retrieved_data(), expected_data)

    def test_zero_page_size_ignored(self):
        """
        If the page size <= 0 then the default fetch size is used.
        """
        cursor = self.prepare()
        self.create_ks(cursor, 'test_paging_size', 2)
        cursor.execute("CREATE TABLE paging_test ( id uuid PRIMARY KEY, value text )")

        def random_txt(text):
            return uuid.uuid1()

        data = """
              | id     |value   |
         *5001| [uuid] |testing |
            """
        expected_data = create_rows(data, cursor, 'paging_test', format_funcs={'id': random_txt, 'value': unicode})

        future = cursor.execute_async(
            SimpleStatement("select * from paging_test", fetch_size=0)
        )

        pf = PageFetcher(future).request_all_pages()

        self.assertEqual(pf.retrieved_pagecount(), 2)
        self.assertEqual(pf.num_results_all_pages(), [5000, 1])

        # make sure expected and actual have same data elements (ignoring order)
        self.assertEqualIgnoreOrder(pf.all_retrieved_data(), expected_data)


class TestPagingWithModifiers(BasePagingTester, PageAssertionMixin):
    """
    Tests concerned with paging when CQL modifiers (such as order, limit, allow filtering) are used.
    """
    def test_with_order_by(self):
        """"
        Paging over a single partition with ordering should work.
        (Spanning multiple partitions won't though, by design. See CASSANDRA-6722).
        """
        cursor = self.prepare()
        self.create_ks(cursor, 'test_paging', 2)
        cursor.execute(
            """
            CREATE TABLE paging_test (
                id int,
                value text,
                PRIMARY KEY (id, value)
            ) WITH CLUSTERING ORDER BY (value ASC)
            """)

        data = """
            |id|value|
            |1 |a    |
            |1 |b    |
            |1 |c    |
            |1 |d    |
            |1 |e    |
            |1 |f    |
            |1 |g    |
            |1 |h    |
            |1 |i    |
            |1 |j    |
            """

        expected_data = create_rows(data, cursor, 'paging_test', format_funcs={'id': int, 'value': unicode})

        future = cursor.execute_async(
            SimpleStatement("select * from paging_test where id = 1 order by value asc", fetch_size=5)
        )

        pf = PageFetcher(future).request_all_pages()

        self.assertEqual(pf.retrieved_pagecount(), 2)
        self.assertEqual(pf.num_results_all_pages(), [5, 5])

        # these should be equal (in the same order)
        self.assertEqual(pf.all_retrieved_data(), expected_data)

        # make sure we don't allow paging over multiple partitions with order because that's weird
        with self.assertRaisesRegexp(InvalidRequest, 'Cannot page queries with both ORDER BY and a IN restriction on the partition key'):
            stmt = SimpleStatement("select * from paging_test where id in (1,2) order by value asc")
            cursor.execute(stmt)

    def test_with_limit(self):
        cursor = self.prepare()
        self.create_ks(cursor, 'test_paging_size', 2)
        cursor.execute("CREATE TABLE paging_test ( id int PRIMARY KEY, value text )")

        data = """
            |id|value           |
            |1 |testing         |
            |2 |and more testing|
            |3 |and more testing|
            |4 |and more testing|
            |5 |and more testing|
            |6 |testing         |
            |7 |and more testing|
            |8 |and more testing|
            |9 |and more testing|
            """
        expected_data = create_rows(data, cursor, 'paging_test', format_funcs={'id': int, 'value': unicode})

        future = cursor.execute_async(
            SimpleStatement("select * from paging_test limit 5", fetch_size=9)
        )

        pf = PageFetcher(future).request_all_pages()

        self.assertEqual(pf.retrieved_pagecount(), 1)
        self.assertEqual(pf.num_results_all_pages(), [5])

        # make sure all the data retrieved is a subset of input data
        self.assertIsSubsetOf(pf.all_retrieved_data(), expected_data)

        # let's do another query with a limit larger than one page
        future = cursor.execute_async(
            SimpleStatement("select * from paging_test limit 8", fetch_size=5)
        )

        pf = PageFetcher(future).request_all_pages()

        self.assertEqual(pf.retrieved_pagecount(), 2)
        self.assertEqual(pf.num_results_all_pages(), [5, 3])
        self.assertIsSubsetOf(pf.all_retrieved_data(), expected_data)

    def test_with_allow_filtering(self):
        cursor = self.prepare()
        self.create_ks(cursor, 'test_paging_size', 2)
        cursor.execute("CREATE TABLE paging_test ( id int, value text, PRIMARY KEY (id, value) )")

        data = """
            |id|value           |
            |1 |testing         |
            |2 |and more testing|
            |3 |and more testing|
            |4 |and more testing|
            |5 |and more testing|
            |6 |testing         |
            |7 |and more testing|
            |8 |and more testing|
            |9 |and more testing|
            """
        create_rows(data, cursor, 'paging_test', format_funcs={'id': int, 'value': unicode})

        future = cursor.execute_async(
            SimpleStatement("select * from paging_test where value = 'and more testing' ALLOW FILTERING", fetch_size=4)
        )

        pf = PageFetcher(future).request_all_pages()

        self.assertEqual(pf.retrieved_pagecount(), 2)
        self.assertEqual(pf.num_results_all_pages(), [4, 3])

        # make sure the allow filtering query matches the expected results (ignoring order)
        self.assertEqualIgnoreOrder(
            pf.all_retrieved_data(),
            parse_data_into_dicts(
                """
                |id|value           |
                |2 |and more testing|
                |3 |and more testing|
                |4 |and more testing|
                |5 |and more testing|
                |7 |and more testing|
                |8 |and more testing|
                |9 |and more testing|
                """, format_funcs={'id': int, 'value': unicode}
            )
        )


class TestPagingData(BasePagingTester, PageAssertionMixin):
    def test_paging_a_single_wide_row(self):
        cursor = self.prepare()
        self.create_ks(cursor, 'test_paging_size', 2)
        cursor.execute("CREATE TABLE paging_test ( id int, value text, PRIMARY KEY (id, value) )")

        def random_txt(text):
            return unicode(uuid.uuid1())

        data = """
              | id | value                  |
        *10000| 1  | [replaced with random] |
            """
        expected_data = create_rows(data, cursor, 'paging_test', format_funcs={'id': int, 'value': random_txt})

        future = cursor.execute_async(
            SimpleStatement("select * from paging_test where id = 1", fetch_size=3000)
        )

        pf = PageFetcher(future).request_all_pages()

        self.assertEqual(pf.retrieved_pagecount(), 4)
        self.assertEqual(pf.num_results_all_pages(), [3000, 3000, 3000, 1000])

        self.assertEqualIgnoreOrder(pf.all_retrieved_data(), expected_data)

    def test_paging_across_multi_wide_rows(self):
        cursor = self.prepare()
        self.create_ks(cursor, 'test_paging_size', 2)
        cursor.execute("CREATE TABLE paging_test ( id int, value text, PRIMARY KEY (id, value) )")

        def random_txt(text):
            return unicode(uuid.uuid1())

        data = """
              | id | value                  |
         *5000| 1  | [replaced with random] |
         *5000| 2  | [replaced with random] |
            """
        expected_data = create_rows(data, cursor, 'paging_test', format_funcs={'id': int, 'value': random_txt})

        future = cursor.execute_async(
            SimpleStatement("select * from paging_test where id in (1,2)", fetch_size=3000)
        )

        pf = PageFetcher(future).request_all_pages()

        self.assertEqual(pf.retrieved_pagecount(), 4)
        self.assertEqual(pf.num_results_all_pages(), [3000, 3000, 3000, 1000])

        self.assertEqualIgnoreOrder(pf.all_retrieved_data(), expected_data)

    def test_paging_using_secondary_indexes(self):
        cursor = self.prepare()
        self.create_ks(cursor, 'test_paging_size', 2)
        cursor.execute("CREATE TABLE paging_test ( id int, mybool boolean, sometext text, PRIMARY KEY (id, sometext) )")
        cursor.execute("CREATE INDEX ON paging_test(mybool)")

        def random_txt(text):
            return unicode(uuid.uuid1())

        def bool_from_str_int(text):
            return bool(int(text))

        data = """
             | id | mybool| sometext |
         *100| 1  | 1     | [random] |
         *300| 2  | 0     | [random] |
         *500| 3  | 1     | [random] |
         *400| 4  | 0     | [random] |
            """
        all_data = create_rows(
            data, cursor, 'paging_test',
            format_funcs={'id': int, 'mybool': bool_from_str_int, 'sometext': random_txt}
        )

        future = cursor.execute_async(
            SimpleStatement("select * from paging_test where mybool = true", fetch_size=400)
        )

        pf = PageFetcher(future).request_all_pages()

        # the query only searched for True rows, so let's pare down the expectations for comparison
        expected_data = filter(lambda x: x.get('mybool') is True, all_data)

        self.assertEqual(pf.retrieved_pagecount(), 2)
        self.assertEqual(pf.num_results_all_pages(), [400, 200])
        self.assertEqualIgnoreOrder(expected_data, pf.all_retrieved_data())


class TestPagingSizeChange(BasePagingTester, PageAssertionMixin):
    """
    Tests concerned with paging when the page size is changed between page retrievals.
    """
    def test_page_size_change(self):
        cursor = self.prepare()
        self.create_ks(cursor, 'test_paging_size', 2)
        cursor.execute("CREATE TABLE paging_test ( id int, sometext text, PRIMARY KEY (id, sometext) )")

        def random_txt(text):
            return unicode(uuid.uuid1())

        data = """
              | id | sometext |
         *2000| 1  | [random] |
            """
        create_rows(data, cursor, 'paging_test', format_funcs={'id': int, 'sometext': random_txt})
        stmt = SimpleStatement("select * from paging_test where id = 1", fetch_size=1000)

        future = cursor.execute_async(stmt)

        pf = PageFetcher(future)

        # first page requested/retrieved automatically so no need to request
        self.assertEqual(pf.retrieved_pagecount(), 1)
        self.assertEqual(pf.num_results(1), 1000)

        stmt.fetch_size = 500

        pf.request_page()
        self.assertEqual(pf.retrieved_pagecount(), 2)
        self.assertEqual(pf.num_results(2), 500)

        stmt.fetch_size = 100

        pf.request_all_pages()
        self.assertEqual(pf.retrieved_pagecount(), 7)
        self.assertEqual(pf.num_results_all_pages(), [1000, 500, 100, 100, 100, 100, 100])

    def test_page_size_set_multiple_times_before(self):
        cursor = self.prepare()
        self.create_ks(cursor, 'test_paging_size', 2)
        cursor.execute("CREATE TABLE paging_test ( id int, sometext text, PRIMARY KEY (id, sometext) )")

        def random_txt(text):
            return unicode(uuid.uuid1())

        data = """
              | id | sometext |
         *2000| 1  | [random] |
            """
        create_rows(data, cursor, 'paging_test', format_funcs={'id': int, 'sometext': random_txt})
        stmt = SimpleStatement("select * from paging_test where id = 1", fetch_size=1000)
        stmt.fetch_size = 100
        stmt.fetch_size = 500

        future = cursor.execute_async(stmt)

        pf = PageFetcher(future).request_all_pages()

        self.assertEqual(pf.retrieved_pagecount(), 4)
        self.assertEqual(pf.num_results_all_pages(), [500, 500, 500, 500])

    def test_page_size_after_results_all_retrieved(self):
        """
        Confirm that page size change does nothing after results are exhausted.
        """
        cursor = self.prepare()
        self.create_ks(cursor, 'test_paging_size', 2)
        cursor.execute("CREATE TABLE paging_test ( id int, sometext text, PRIMARY KEY (id, sometext) )")

        def random_txt(text):
            return unicode(uuid.uuid1())

        data = """
              | id | sometext |
         *2000| 1  | [random] |
            """
        create_rows(data, cursor, 'paging_test', format_funcs={'id': int, 'sometext': random_txt})
        stmt = SimpleStatement("select * from paging_test where id = 1", fetch_size=500)

        future = cursor.execute_async(stmt)

        pf = PageFetcher(future)

        pf.request_all_pages()
        self.assertEqual(pf.retrieved_pagecount(), 4)
        self.assertEqual(pf.num_results_all_pages(), [500, 500, 500, 500])

        stmt.fetch_size = 200
        pf.request_page()
        self.assertEqual(pf.num_results_all_pages(), [500, 500, 500, 500])


class TestPagingDatasetChanges(BasePagingTester, PageAssertionMixin):
    """
    Tests concerned with paging when the queried dataset changes while pages are being retrieved.
    """
    def test_data_change_impacting_earlier_page(self):
        cursor = self.prepare()
        self.create_ks(cursor, 'test_paging_size', 2)
        cursor.execute("CREATE TABLE paging_test ( id int, mytext text, PRIMARY KEY (id, mytext) )")

        def random_txt(text):
            return unicode(uuid.uuid1())

        data = """
              | id | mytext   |
          *500| 1  | [random] |
          *500| 2  | [random] |
            """
        expected_data = create_rows(data, cursor, 'paging_test', format_funcs={'id': int, 'mytext': random_txt})

        # get 501 rows so we have definitely got the 1st row of the second partition
        future = cursor.execute_async(
            SimpleStatement("select * from paging_test where id in (1,2)", fetch_size=501)
        )

        pf = PageFetcher(future)
        # no need to request page here, because the first page is automatically retrieved

        # we got one page and should be done with the first partition (for id=1)
        # let's add another row for that first partition (id=1) and make sure it won't sneak into results
        cursor.execute(SimpleStatement("insert into paging_test (id, mytext) values (1, 'foo')"))

        pf.request_all_pages()
        self.assertEqual(pf.retrieved_pagecount(), 2)
        self.assertEqual(pf.num_results_all_pages(), [501, 499])

        self.assertEqualIgnoreOrder(pf.all_retrieved_data(), expected_data)

    def test_data_change_impacting_later_page(self):
        cursor = self.prepare()
        self.create_ks(cursor, 'test_paging_size', 2)
        cursor.execute("CREATE TABLE paging_test ( id int, mytext text, PRIMARY KEY (id, mytext) )")

        def random_txt(text):
            return unicode(uuid.uuid1())

        data = """
              | id | mytext   |
          *500| 1  | [random] |
          *499| 2  | [random] |
            """
        expected_data = create_rows(data, cursor, 'paging_test', format_funcs={'id': int, 'mytext': random_txt})

        future = cursor.execute_async(
            SimpleStatement("select * from paging_test where id in (1,2)", fetch_size=500)
        )

        pf = PageFetcher(future)
        # no need to request page here, because the first page is automatically retrieved

        # we've already paged the first partition, but adding a row for the second (id=2)
        # should still result in the row being seen on the subsequent pages
        cursor.execute(SimpleStatement("insert into paging_test (id, mytext) values (2, 'foo')"))

        pf.request_all_pages()
        self.assertEqual(pf.retrieved_pagecount(), 2)
        self.assertEqual(pf.num_results_all_pages(), [500, 500])

        # add the new row to the expected data and then do a compare
        expected_data.append({u'id': 2, u'mytext': u'foo'})
        self.assertEqualIgnoreOrder(pf.all_retrieved_data(), expected_data)

    def test_data_delete_removing_remainder(self):
        cursor = self.prepare()
        self.create_ks(cursor, 'test_paging_size', 2)
        cursor.execute("CREATE TABLE paging_test ( id int, mytext text, PRIMARY KEY (id, mytext) )")

        def random_txt(text):
            return unicode(uuid.uuid1())

        data = """
              | id | mytext   |
          *500| 1  | [random] |
          *500| 2  | [random] |
            """

        create_rows(data, cursor, 'paging_test', format_funcs={'id': int, 'mytext': random_txt})

        future = cursor.execute_async(
            SimpleStatement("select * from paging_test where id in (1,2)", fetch_size=500)
        )

        pf = PageFetcher(future)
        # no need to request page here, because the first page is automatically retrieved

        # delete the results that would have shown up on page 2
        cursor.execute(SimpleStatement("delete from paging_test where id = 2"))

        pf.request_all_pages()
        self.assertEqual(pf.retrieved_pagecount(), 1)
        self.assertEqual(pf.num_results_all_pages(), [500])

    def test_row_TTL_expiry_during_paging(self):
        cursor = self.prepare()
        self.create_ks(cursor, 'test_paging_size', 2)
        cursor.execute("CREATE TABLE paging_test ( id int, mytext text, PRIMARY KEY (id, mytext) )")

        def random_txt(text):
            return unicode(uuid.uuid1())

        # create rows with TTL (some of which we'll try to get after expiry)
        create_rows(
            """
                | id | mytext   |
            *300| 1  | [random] |
            *400| 2  | [random] |
            """,
            cursor, 'paging_test', format_funcs={'id': int, 'mytext': random_txt}, postfix='USING TTL 10'
        )

        # create rows without TTL
        create_rows(
            """
                | id | mytext   |
            *500| 3  | [random] |
            """,
            cursor, 'paging_test', format_funcs={'id': int, 'mytext': random_txt}
        )

        future = cursor.execute_async(
            SimpleStatement("select * from paging_test where id in (1,2,3)", fetch_size=300)
        )

        pf = PageFetcher(future)
        # no need to request page here, because the first page is automatically retrieved
        # this page will be partition id=1, it has TTL rows but they are not expired yet

        # sleep so that the remaining TTL rows from partition id=2 expire
        time.sleep(15)

        pf.request_all_pages()
        self.assertEqual(pf.retrieved_pagecount(), 3)
        self.assertEqual(pf.num_results_all_pages(), [300, 300, 200])

    def test_cell_TTL_expiry_during_paging(self):
        cursor = self.prepare()
        self.create_ks(cursor, 'test_paging_size', 2)
        cursor.execute("""
            CREATE TABLE paging_test (
                id int,
                mytext text,
                somevalue text,
                anothervalue text,
                PRIMARY KEY (id, mytext) )
            """)

        def random_txt(text):
            return unicode(uuid.uuid1())

        data = create_rows(
            """
                | id | mytext   | somevalue | anothervalue |
            *500| 1  | [random] | foo       |  bar         |
            *500| 2  | [random] | foo       |  bar         |
            *500| 3  | [random] | foo       |  bar         |
            """,
            cursor, 'paging_test', format_funcs={'id': int, 'mytext': random_txt}
        )

        future = cursor.execute_async(
            SimpleStatement("select * from paging_test where id in (1,2,3)", fetch_size=500)
        )

        pf = PageFetcher(future)

        # no need to request page here, because the first page is automatically retrieved
        page1 = pf.retrieved_page_data(1)
        self.assertEqualIgnoreOrder(page1, data[:500])

        # set some TTLs for data on page 3
        for row in data[1000:1500]:
            _id, mytext = row['id'], row['mytext']
            stmt = SimpleStatement("""
                update paging_test using TTL 10
                set somevalue='one', anothervalue='two' where id = {id} and mytext = '{mytext}'
                """.format(id=_id, mytext=mytext)
            )
            cursor.execute(stmt)

        # check page two
        pf.request_page()
        page2 = pf.retrieved_page_data(2)
        self.assertEqualIgnoreOrder(page2, data[500:1000])

        page3expected = []
        for row in data[1000:1500]:
            _id, mytext = row['id'], row['mytext']
            page3expected.append(
                {u'id': _id, u'mytext': mytext, u'somevalue': None, u'anothervalue': None}
            )

        time.sleep(15)

        pf.request_page()
        page3 = pf.retrieved_page_data(3)
        self.assertEqualIgnoreOrder(page3, page3expected)

    def test_node_unavailabe_during_paging(self):
        cluster = self.cluster
        cluster.populate(3).start()
        node1, node2, node3 = cluster.nodelist()
        cursor = self.cql_connection(node1)
        self.create_ks(cursor, 'test_paging_size', 1)
        cursor.execute("CREATE TABLE paging_test ( id uuid, mytext text, PRIMARY KEY (id, mytext) )")

        def make_uuid(text):
            return uuid.uuid4()

        create_rows(
            """
                  | id      | mytext |
            *10000| [uuid]  | foo    |
            """,
            cursor, 'paging_test', format_funcs={'id': make_uuid}
        )

        future = cursor.execute_async(
            SimpleStatement("select * from paging_test where mytext = 'foo' allow filtering", fetch_size=2000)
        )

        pf = PageFetcher(future)
        # no need to request page here, because the first page is automatically retrieved

        # stop a node and make sure we get an error trying to page the rest
        node1.stop()
        with self.assertRaisesRegexp(RuntimeError, 'Requested pages were not delivered before timeout'):
            pf.request_all_pages()

        # TODO: can we resume the node and expect to get more results from the result set or is it done?


class TestPagingQueryIsolation(BasePagingTester, PageAssertionMixin):
    """
    Tests concerned with isolation of paged queries (queries can't affect each other).
    """
    def test_query_isolation(self):
        """
        Interleave some paged queries and make sure nothing bad happens.
        """
        cursor = self.prepare()
        self.create_ks(cursor, 'test_paging_size', 2)
        cursor.execute("CREATE TABLE paging_test ( id int, mytext text, PRIMARY KEY (id, mytext) )")

        def random_txt(text):
            return unicode(uuid.uuid1())

        data = """
               | id | mytext   |
          *5000| 1  | [random] |
          *5000| 2  | [random] |
          *5000| 3  | [random] |
          *5000| 4  | [random] |
          *5000| 5  | [random] |
          *5000| 6  | [random] |
          *5000| 7  | [random] |
          *5000| 8  | [random] |
          *5000| 9  | [random] |
          *5000| 10 | [random] |
            """
        expected_data = create_rows(data, cursor, 'paging_test', format_funcs={'id': int, 'mytext': random_txt})

        stmts = [
            SimpleStatement("select * from paging_test where id in (1)", fetch_size=500),
            SimpleStatement("select * from paging_test where id in (2)", fetch_size=600),
            SimpleStatement("select * from paging_test where id in (3)", fetch_size=700),
            SimpleStatement("select * from paging_test where id in (4)", fetch_size=800),
            SimpleStatement("select * from paging_test where id in (5)", fetch_size=900),
            SimpleStatement("select * from paging_test where id in (1)", fetch_size=1000),
            SimpleStatement("select * from paging_test where id in (2)", fetch_size=1100),
            SimpleStatement("select * from paging_test where id in (3)", fetch_size=1200),
            SimpleStatement("select * from paging_test where id in (4)", fetch_size=1300),
            SimpleStatement("select * from paging_test where id in (5)", fetch_size=1400),
            SimpleStatement("select * from paging_test where id in (1,2,3,4,5,6,7,8,9,10)", fetch_size=1500)
        ]

        page_fetchers = []

        for stmt in stmts:
            future = cursor.execute_async(stmt)
            page_fetchers.append(PageFetcher(future))
            # first page is auto-retrieved, so no need to request it

        for pf in page_fetchers:
            pf.request_page()

        for pf in page_fetchers:
            pf.request_page()

        for pf in page_fetchers:
            pf.request_all_pages()

        self.assertEqual(page_fetchers[0].retrieved_pagecount(), 10)
        self.assertEqual(page_fetchers[1].retrieved_pagecount(), 9)
        self.assertEqual(page_fetchers[2].retrieved_pagecount(), 8)
        self.assertEqual(page_fetchers[3].retrieved_pagecount(), 7)
        self.assertEqual(page_fetchers[4].retrieved_pagecount(), 6)
        self.assertEqual(page_fetchers[5].retrieved_pagecount(), 5)
        self.assertEqual(page_fetchers[6].retrieved_pagecount(), 5)
        self.assertEqual(page_fetchers[7].retrieved_pagecount(), 5)
        self.assertEqual(page_fetchers[8].retrieved_pagecount(), 4)
        self.assertEqual(page_fetchers[9].retrieved_pagecount(), 4)
        self.assertEqual(page_fetchers[10].retrieved_pagecount(), 34)

        self.assertEqualIgnoreOrder(page_fetchers[0].all_retrieved_data(), expected_data[:5000])
        self.assertEqualIgnoreOrder(page_fetchers[1].all_retrieved_data(), expected_data[5000:10000])
        self.assertEqualIgnoreOrder(page_fetchers[2].all_retrieved_data(), expected_data[10000:15000])
        self.assertEqualIgnoreOrder(page_fetchers[3].all_retrieved_data(), expected_data[15000:20000])
        self.assertEqualIgnoreOrder(page_fetchers[4].all_retrieved_data(), expected_data[20000:25000])
        self.assertEqualIgnoreOrder(page_fetchers[5].all_retrieved_data(), expected_data[:5000])
        self.assertEqualIgnoreOrder(page_fetchers[6].all_retrieved_data(), expected_data[5000:10000])
        self.assertEqualIgnoreOrder(page_fetchers[7].all_retrieved_data(), expected_data[10000:15000])
        self.assertEqualIgnoreOrder(page_fetchers[8].all_retrieved_data(), expected_data[15000:20000])
        self.assertEqualIgnoreOrder(page_fetchers[9].all_retrieved_data(), expected_data[20000:25000])
        self.assertEqualIgnoreOrder(page_fetchers[10].all_retrieved_data(), expected_data[:50000])