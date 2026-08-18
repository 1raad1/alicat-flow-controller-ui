"""Combustion calculations independent of the user interface."""


class CombustionCalculator:
    # Stoichiometric O2 demand per mole of fuel:
    #   CH4 + 2 O2    -> CO2 + 2 H2O    (2.00 mol O2 / mol CH4)
    #   2 H2 + O2     -> 2 H2O          (0.50 mol O2 / mol H2)
    #   4 NH3 + 3 O2  -> 2 N2 + 6 H2O   (0.75 mol O2 / mol NH3)
    # Oxygen balance for the combined CH4/H2/NH3 + air reaction
    # (Note 8 Feb 2023):  0.21 * a = 2*CH4 + 0.5*H2 + 0.75*NH3
    O2_PER_CH4 = 2.00
    O2_PER_NH3 = 0.75
    O2_PER_H2 = 0.50
    O2_IN_AIR = 0.21

    def stoich_air(self, nh3_flow, h2_flow, ch4_flow=0.0):
        o2 = (self.O2_PER_NH3 * nh3_flow
              + self.O2_PER_H2 * h2_flow
              + self.O2_PER_CH4 * ch4_flow)
        return o2 / self.O2_IN_AIR

    def phi(self, nh3_flow, h2_flow, air_flow, ch4_flow=0.0):
        if air_flow <= 0:
            return 0.0
        air_s = self.stoich_air(nh3_flow, h2_flow, ch4_flow)
        return air_s / air_flow if air_s > 0 else 0.0
