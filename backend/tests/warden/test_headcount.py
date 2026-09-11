

class TestAnOvercountASiteChoosesToAbsorb:
    """`overcount_tolerance` defaults to zero and the policy argues it should
    stay there: a system that thinks one more person is safe than actually is
    has made the error that kills somebody.

    A site may raise it anyway, and `__post_init__` says the allowance will not
    be silent. It was: a warden counting 38 against a system's 40 was told "No
    action. Record the count and continue." while two people were missing, and
    the drill validated clean because `is_mismatch` was False.
    """

    def permissive(self, physical, system, tolerance=2):
        from app.warden.headcount import Headcount, HeadcountPolicy

        return Headcount(
            zone_id="assembly-north", warden_id="warden-7", device_id="d",
            ts_ms=0, physical_count=physical, system_count=system,
            policy=HeadcountPolicy(undercount_tolerance=2,
                                   overcount_tolerance=tolerance,
                                   calibrated=False, source="a site decided"))

    def test_the_absorbed_people_are_counted(self):
        assert self.permissive(38, 40).tolerated_overcount == 2

    def test_the_advice_says_who_is_still_missing(self):
        advice = self.permissive(38, 40).recommended_action()
        assert "no action is required of you" in advice
        assert "2 person(s)" in advice
        assert "not in front of you" in advice

    def test_a_clean_count_still_says_no_action(self):
        assert self.permissive(40, 40).tolerated_overcount == 0
        assert self.permissive(40, 40).recommended_action().startswith("No action")

    def test_an_undercount_is_not_an_absorbed_overcount(self):
        # People present the system does not know about are a different
        # problem, and tagging them is the fix.
        assert self.permissive(42, 40).tolerated_overcount == 0

    def test_the_default_policy_absorbs_nothing(self):
        from app.warden.headcount import Headcount

        strict = Headcount(zone_id="z", warden_id="w", device_id="d", ts_ms=0,
                           physical_count=38, system_count=40)
        assert strict.tolerated_overcount == 0
        assert strict.is_mismatch is True
