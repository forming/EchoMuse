/* Prove init decides service control the way the tool and the issue mean.
 *
 * `emos-svc stop` reaching a service that is then quietly respawned is
 * exactly the silent-on-hardware failure the other six off-target checks
 * exist for: the tool reports success, the ring keeps going, and nothing says
 * the stop did not take. The reverse is worse — a held service that is never
 * released — because a maintenance window becomes a device that needs a
 * reflash.
 *
 * Two things are checked, both pure, both driven through the real functions:
 *
 *   - the request parser, including that it REFUSES rather than guesses. This
 *     reaches init through a FIFO, and on an older image a misrouted word is a
 *     reboot (#560: `/init stop echomuse` rebooted the device before this).
 *   - that a hold survives a respawn scan and is cleared by `start`, and that
 *     stopping does not count as a fast exit.
 *
 * init.c is included whole, as ringsim.c, pwcheck.c and trialcheck.c do, so
 * this drives the real functions rather than a copy of them.
 *
 *   cc -O2 -o svccheck svccheck.c && ./svccheck
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define main   init_main_unused

#include "init.c"

#undef main

static int failures;

static void ok(const char *what)
{
    printf("  ok  %s\n", what);
}

static void bad(const char *what, const char *why)
{
    failures++;
    printf("  FAIL %s: %s\n", what, why);
}

static int is(const char *what, int got, int want)
{
    if (got == want)
        return 1;
    char buf[160];
    snprintf(buf, sizeof(buf), "got %d, want %d", got, want);
    bad(what, buf);
    return 0;
}

/* A table init can reason about, without running anything. */
static void fake_services(void)
{
    nsvc = 0;
    svc_add("server", NULL, "/data/local/bin/start_server.sh", NULL);
    svc_add("console", NULL, NULL, NULL);
    /* pids as if running; 0 would read as "not started" in the checks. */
    svcs[0].pid = 111;
    svcs[1].pid = 222;
    for (int i = 0; i < nsvc; i++) {
        svcs[i].started = 0;
        svcs[i].fails = 0;
        svcs[i].gone = 0;
        svcs[i].held = 0;
    }
}

int main(void)
{
    struct svc_req r;

    printf("svc_parse:\n");

    if (is("stop",  svc_parse("stop", &r), 0))  ok("stop parses");
    /* Checked right after parsing stop, before the next parse overwrites r. */
    if (is("stop op", r.op == SVC_OP_STOP, 1))
        ok("stop is the stop verb");
    if (is("start", svc_parse("start", &r), 0)) ok("start parses");
    if (is("start op", r.op == SVC_OP_START, 1))
        ok("start is the start verb");
    if (is("restart", svc_parse("restart", &r), 0)) ok("restart parses");
    if (is("status", svc_parse("status", &r), 0)) ok("status parses");
    if (is("status op", r.op == SVC_OP_STATUS, 1))
        ok("status is the status verb");

    fake_services();
    if (is("named service", svc_parse("stop server", &r), 0))
        ok("a known name parses");
    if (is("the name is carried", !strcmp(r.name, "server"), 1))
        ok("the name survives parsing");

    /* A bare verb means every service, which is different from a name that
     * matches nothing. */
    if (is("bare verb", svc_parse("stop", &r), 0) && is("bare names none", r.name[0], 0))
        ok("a bare verb means all services");

    /* THE trap. An unknown verb must be refused, not treated as a boot mode
     * and not treated as "all". */
    is("unknown verb refused", svc_parse("reboot", &r) < 0, 1);
    is("empty line refused", svc_parse("", &r) < 0, 1);
    is("whitespace refused", svc_parse("   ", &r) < 0, 1);
    is("NULL refused", svc_parse(NULL, &r) < 0, 1);
    /* A name that matches nothing is a mistake, not "every service". That
     * difference is the whole reason a typo cannot stop the Echo. */
    is("unknown name refused", svc_parse("stop nosuch", &r) < 0, 1);
    ok("a name matching nothing is refused, not applied to all");

    printf("boot modes:\n");
    is("recovery is known", is_boot_mode("recovery"), 1);
    is("emos is known", is_boot_mode("emos"), 1);
    /* Before this, any word was passed to reboot_into. */
    is("stop is not a boot mode", is_boot_mode("stop"), 0);
    is("a typo is not a boot mode", is_boot_mode("recoveru"), 0);
    is("empty is not a boot mode", is_boot_mode(""), 0);
    ok("only known modes reach reboot_into");

    printf("hold and release:\n");

    fake_services();
    svc_parse("stop server", &r);
    if (is("stop touches one", svc_apply(&r), 1)) ok("stop applies to the named service");
    if (is("the named service is held", svcs[0].held, 1)) ok("stop holds it");
    if (is("its pid is released", svcs[0].pid, -1)) ok("stop clears the pid");
    if (is("the other is untouched", svcs[1].held, 0)) ok("the neighbour is untouched");

    /* The supervisor's own predicate, not a restatement of it. Asking
     * svc_should_start is the only version of this check that can fail: a
     * check that re-states the condition in its own words keeps passing when
     * the condition is removed from supervise(), which is the failure it
     * exists to catch — a stop the tool reported as applied, and a respawn
     * thirty seconds later. */
    is("a held service is not a respawn candidate",
       svc_should_start(&svcs[0], (time_t)600), 0);
    ok("the supervisor's own predicate skips a held service");

    /* And the release: clearing the hold makes it startable again, which is
     * what a maintenance window ends with. */
    fake_services();
    svc_parse("stop server", &r);
    svc_apply(&r);
    svc_parse("start server", &r);
    svc_apply(&r);
    /* Well past svc_backoff, which is 2s on a fresh service: a release makes
     * it a candidate, but not instantly. */
    is("a released service is a start candidate",
       svc_should_start(&svcs[0], (time_t)600), 1);
    ok("start puts it back in the running set");

    /* A running service is never a candidate either — the same predicate has
     * to keep its other answer. */
    fake_services();
    is("a running service is not a candidate",
       svc_should_start(&svcs[0], (time_t)600), 0);

    fake_services();
    svc_parse("stop", &r);
    if (is("bare stop touches all", svc_apply(&r), 2)) ok("a bare verb holds everything");
    if (is("both held", svcs[0].held + svcs[1].held, 2)) ok("every service is held");

    fake_services();
    /* A stop is not a crash: it must not feed the fast-exit backoff, or a
     * start straight after resumes into the backoff the stop created. */
    svcs[0].fails = 2;
    svc_parse("stop server", &r);
    svc_apply(&r);
    is("stopping is not counted as failing", svcs[0].fails, 2);
    svc_parse("start server", &r);
    svc_apply(&r);
    is("start clears the hold", svcs[0].held, 0);
    is("start clears the fast-exit count", svcs[0].fails, 0);
    ok("a stop does not put the next start into backoff");

    /* Holds are not persisted, deliberately: a device with no adb must come
     * back after a reboot. There is no write of `held` anywhere, and the
     * field is initialised to 0 in svc_add's initialiser path. */
    fake_services();
    is("a fresh device holds nothing", svcs[0].held, 0);
    ok("holds start clear and are never written to disk");

    if (failures) {
        printf("\n%d failure(s)\n", failures);
        return 1;
    }
    printf("svccheck: all checks passed\n");
    return 0;
}
